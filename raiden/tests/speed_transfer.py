# -*- coding: utf8 -*-
from __future__ import print_function

import time
from collections import namedtuple

import gevent
import GreenletProfiler
from ethereum import slogging

from raiden.app import create_network
from raiden import profiling
from raiden.tasks import TransferTask
from raiden.transport import UDPTransport

try:
    from termcolor import colored
except ImportError:
    def colored(text, color=None, on_color=None, attrs=None):
        return text


slogging.configure(':CRITICAL')

ProfileLine = namedtuple(
    'ProfileLine',
    (
        'recursion',
        'name',
        'calls',
        'cumulative',
        'total',
        'average',
    )
)

FORMAT_LINE = '{total:>6.4f} {cumulative:>6.4f} {avg:>6.4f} {align}{name} [{calls} calls]'


def print_stats(stat_list, total_time):
    # The GreenletProfiler is based on YAPPI, the YAPPI implementation does
    # quite a bit of calling back and forth from C to Python, the overall
    # result is as follows:
    #
    # [GreenletProfiler:start]  Registers YAPPI to profile all threads
    #                           (threading.setprofile and _yapp.start)
    # [_yappi.c:profile_event]  at each event a callback is called
    #   [_yappi.c:_call_enter/_call_leve] and the callstack is traced
    #
    # The yappi implementation can use three different clocks, cpu, rusage or
    # wall [timing.c:tickcount], there a couple of things that needs to be kept
    # track of:
    #
    # - context switches, the clock needs to be paused and restore.
    #  - If the GreenletProfiler is running for cpu time it will take care of
    #  _not_ accounting for the cpu time of other threads (it will compensate
    #  by artificially increasing the start time by the time the thread was
    #  sleeping)
    #
    # The traced data can be extract in two ways, through the enum_func_stats
    # [_yappi.c:_pitenumstat] or enum_thread_stats [_yappi.c:_ctxenumstat], each
    # will enumerate through the traced data and run a callback from C to
    # Python

    # we are trying to reconstruct the call-stack, this is used to track if
    # there are rootless calls
    call_count = {}

    nameline_stat = {}
    for stat in stat_list:
        # we cant use just name because labmda's are named "<lambda>" and will conflict
        key = (stat.name, stat.lineno)

        if key in nameline_stat:
            raise Exception('the code assumes that (name, lineno) are unique, they are not')

        nameline_stat[key] = stat

    # index is a counter increment by one every time a new function is called,
    # so it is _somewhat_ in order
    ordered_stats = sorted(stat_list, key=lambda item: item.index)

    # we need to recursivelly format the call-stack, this function is also
    # closing over variables and changing state
    def _stack(stat, recursion=0):
        key = (stat.name, stat.lineno)
        accumulated_count = call_count.get(key, 0)

        if accumulated_count >= stat.ncall:
            return []

        call_count[key] = accumulated_count + stat.ncall

        line = ProfileLine(
            recursion=recursion,
            name=stat.name,
            calls=stat.ncall,
            cumulative=stat.tsub,
            total=stat.ttot,
            average=stat.tavg,
        )

        stack = [line]

        if stat.children is not None:
            for child in stat.children:
                if child.name.endswith('switch'):
                    continue

                child_key = (child.name, child.lineno)
                child_line = _stack(nameline_stat[child_key], recursion + 1)
                stack.extend(child_line)

        return stack

    def _line(recursion):
        line = list(' ' * (recursion + 1))
        line[7::7] = len(line[7::7]) * '.'
        return ''.join(line)

    highest_time = 0
    full_stack = []
    for stat in ordered_stats:
        for line in _stack(stat):
            highest_time = max(highest_time, line.average)
            full_stack.append(line)

    cumulative_depth = float('inf')
    formated_stack = []

    print(' total   cumm single')
    for line in full_stack:

        formated_line = FORMAT_LINE.format(
            align=_line(line.recursion),
            name=line.name,
            calls=line.calls,
            total=line.total,
            cumulative=line.cumulative,
            avg=line.average,
        )

        # highlight slowest blocks
        if line.cumulative > total_time * 0.1:
            cumulative_depth = line.recursion

        if cumulative_depth >= line.recursion:
            cumulative_depth = float('inf')

        # highlight the slowest functions
        if highest_time * 0.85 <= line.average:
            formated_line = colored(formated_line, 'red')
        elif cumulative_depth <= line.recursion:
            formated_line = colored(formated_line, 'blue')

        # hide functions that wont save time after optimizing ...
        # if line.cumulative < 0.0001:
        #     continue

        formated_stack.append(formated_line)

    print('\n'.join(formated_stack))
    print('''
    total  - total wall time to run the function call (including subcalls)
    cumm   - total wall time for the function itself (removing subcalls)
    single - time spent on a _single_ execution (average time, really)
    ''')
    print('Total time: {:6.4f}s'.format(total_time))


def profile_transfer(num_nodes=10, channels_per_node=2):
    num_assets = 1

    all_apps = create_network(
        num_nodes=num_nodes,
        num_assets=num_assets,
        channels_per_node=channels_per_node,
        transport_class=UDPTransport,
    )

    main_app = all_apps[0]
    main_api = main_app.raiden.api

    # channels
    assets = sorted(main_app.raiden.assetmanagers.keys())
    asset = assets[0]
    main_assetmanager = main_app.raiden.assetmanagers[asset]

    # search for a path of length=2 A > B > C
    num_hops = 2
    source = main_app.raiden.address
    paths = main_assetmanager.channelgraph.get_paths_of_length(source, num_hops)

    # sanity check
    assert len(paths)

    path = paths[0]
    target = path[-1]
    hops_length = min(len(p) for p in main_assetmanager.channelgraph.get_paths(source, target))

    for p in paths:
        assert len(p) == num_hops + 1
        assert p[0] == source

    assert path in main_assetmanager.channelgraph.get_paths(source, target)
    assert hops_length == num_hops + 1

    assetmanagers_by_address = dict(
        (app.raiden.address, app.raiden.assetmanagers)
        for app in all_apps
    )

    # addresses
    a, b, c = path
    asset_address = main_assetmanager.asset_address

    amount = 10
    finished = gevent.event.Event()

    def signal_end(task, success):
        finished.set()

    # set shorter timeout for testing
    TransferTask.timeout_per_hop = 0.1
    destiny_assetmanager = assetmanagers_by_address[b][asset_address]
    destiny_assetmanager.transfermanager.on_task_completed_callbacks.append(signal_end)

    # GreenletProfiler.set_clock_type('cpu')

    # measure the hot path
    with profiling.profile():
        main_api.transfer(asset_address, amount, target)
        gevent.wait([finished])

    profiling.print_all_threads()
    # profiling.print_merged()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--nodes', default=10, type=int)
    parser.add_argument('--channels-per-node', default=2, type=int)

    args = parser.parse_args()

    profile_transfer(
        num_nodes=args.nodes,
        channels_per_node=args.channels_per_node,
    )
