# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2016, ARM Limited and contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import pandas as pd
import logging

from bart.common.Utils import select_window, area_under_curve
from bart.sched import pelt
from devlib.utils.misc import memoized
from env import TestEnv
from executor import Executor
from test import LisaTest, experiment_test
from time import sleep
from trace import Trace
from trappy.stats.grammar import Parser
from wlgen.utils import SchedEntity, Task, Taskgroup

UTIL_SCALE = 1024
# Time in seconds to allow for util_avg to converge (i.e. ignored time)
UTIL_AVG_CONVERGENCE_TIME = 0.3
# Allowed margin between expected and observed util_avg value
ERROR_MARGIN_PCT = 15
# PELT half-life value in ms
HALF_LIFE_MS = 32

class _LoadTrackingBase(LisaTest):
    """Base class for shared functionality of load tracking tests"""
    test_conf = {
        'tools'    : [ 'rt-app' ],
        'ftrace' : {
            'events' : [
                'sched_switch',
                'sched_load_avg_task',
                'sched_load_avg_cpu',
                'sched_pelt_se',
                'sched_load_se',
                'sched_load_cfs_rq',
            ],
        },
        # cgroups required by freeze_userspace flag
        'modules': ['cpufreq', 'cgroups'],
    }

    @memoized
    @staticmethod
    def _get_cpu_capacity(test_env, cpu):
        if test_env.nrg_model:
            return test_env.nrg_model.get_cpu_capacity(cpu)

        return test_env.target.read_int(
            '/sys/devices/system/cpu/cpu{}/cpu_capacity'.format(cpu))

    @classmethod
    def setUpClass(cls, *args, **kwargs):
        super(_LoadTrackingBase, cls).runExperiments(*args, **kwargs)

    @classmethod
    def get_wload(cls, cpu, duty_cycle_pct):
        """
        Get a specification for a rt-app workload with the specificied duty
        cycle, pinned to the given CPU.

        :param cpu: CPU where to pin the task
        :type cpu: int

        :param duty_cycle_pct: duty cycle of the workload
        :type duty_cycle_pct: int
        """
        return {
            'type' : 'rt-app',
                'conf' : {
                    'class' : 'periodic',
                    'params' : {
                        'duty_cycle_pct': duty_cycle_pct,
                        'duration_s': 2,
                        'period_ms': 16,
                    },
                    'tasks' : 1,
                    'prefix' : 'lt_test',
                    'cpus' : [cpu]
                },
            }

    def get_expected_util_avg(self, experiment):
        """
        Examine trace to figure out an expected mean for util_avg

        Assumes an RT-App workload with a single task with a single phase
        """
        # Find duty cycle of the experiment's workload task
        [task] = experiment.wload.tasks.keys()
        sched_assert = self.get_sched_assert(experiment, task)
        duty_cycle_pct = sched_assert.getDutyCycle(self.get_window(experiment))

        # Find the (max) capacity of the CPU the workload was run on
        [cpu] = experiment.wload.cpus
        cpu_capacity = self._get_cpu_capacity(self.te, cpu)

        # Scale the capacity linearly according to the frequency the workload
        # was run at
        cpufreq = experiment.conf['cpufreq']
        if cpufreq['governor'] == 'userspace':
            freq = cpufreq['freqs'][cpu]
            max_freq = max(self.te.target.cpufreq.list_frequencies(cpu))
            cpu_capacity *= float(freq) / max_freq
        else:
            assert cpufreq['governor'] == 'performance'

        # Scale the relative CPU/freq capacity into the range 0..1
        scale = max(self._get_cpu_capacity(self.te, cpu)
                    for cpu in range(self.te.target.number_of_cpus))
        scaling_factor = float(cpu_capacity) / scale

        return UTIL_SCALE * (duty_cycle_pct / 100.) * scaling_factor

    def get_sched_task_signals(self, experiment, signals):
        """
        Get a pandas.DataFrame with the sched signals for the workload task

        This examines scheduler load tracking trace events, supporting either
        sched_load_avg_task or sched_pelt_se. You will need a target kernel that
        includes these events.

        :param experiment: Experiment to get trace for
        :param signals: List of load tracking signals to extract. Probably a
                        subset of ``['util_avg', 'load_avg']``
        :returns: :class:`pandas.DataFrame` with a column for each signal for
                  the experiment's workload task
        """
        [task] = experiment.wload.tasks.keys()
        trace = self.get_trace(experiment)

        # There are two different scheduler trace events that expose the load
        # tracking signals. Neither of them is in mainline. Eventually they
        # should be unified but for now we'll just check for both types of
        # event.
        # TODO: Add support for this parsing in Trappy and/or tasks_analysis
        signal_fields = signals
        if 'sched_load_avg_task' in trace.available_events:
            event = 'sched_load_avg_task'
        elif 'sched_load_se' in trace.available_events:
            event = 'sched_load_se'
            # sched_load_se uses 'util' and 'load' instead of 'util_avg' and
            # 'load_avg'
            signal_fields = [s.replace('_avg', '') for s in signals]
        elif 'sched_pelt_se' in trace.available_events:
            event = 'sched_pelt_se'
        else:
            raise ValueError('No sched_load_avg_task or sched_pelt_se events. '
                             'Does the kernel support them?')

        df = getattr(trace.ftrace, event).data_frame
        df = df[df['comm'] == task][signal_fields]
        df = select_window(df, self.get_window(experiment))
        return df.rename(columns=dict(zip(signal_fields, signals)))

    def get_signal_mean(self, experiment, signal,
                        ignore_first_s=UTIL_AVG_CONVERGENCE_TIME):
        """
        Get the mean of a scheduler signal for the experiment's task

        Ignore the first `ignore_first_s` seconds of the signal.
        """
        (wload_start, wload_end) = self.get_window(experiment)
        window = (wload_start + ignore_first_s, wload_end)

        signal = self.get_sched_task_signals(experiment, [signal])[signal]
        signal = select_window(signal, window)
        return area_under_curve(signal) / (window[1] - window[0])

    def get_simulated_pelt(self, experiment, task, init_value):
        """
        Get simulated PELT signal and the periodic task used to model it.

        :returns: tuple of
            - :mod:`bart.sched.pelt.Simulator` the PELT simulator object
            - :mod:`bart.sched.pelt.PeriodicTask` simulated periodic task
            - :mod:`pandas.DataFrame` instance which reports the computed
                    PELT values at each PELT sample interval.
        """
        phase = experiment.wload.params['profile'][task]['phases'][0]
        pelt_task = pelt.PeriodicTask(period_samples=phase.period_ms,
                                      duty_cycle_pct=phase.duty_cycle_pct)
        peltsim = pelt.Simulator(init_value=init_value,
                                 half_life_ms=HALF_LIFE_MS)
        df = peltsim.getSignal(pelt_task, 0, phase.duration_s + 1)
        return peltsim, pelt_task, df


class FreqInvarianceTest(_LoadTrackingBase):
    """
    Goal
    ====
    Basic check for frequency invariant load tracking

    Detailed Description
    ====================
    This test runs the same workload on the most capable CPU on the system at a
    cross section of available frequencies. The trace is then examined to find
    the average activation length of the workload, which is combined with the
    known period to estimate an expected mean value for util_avg for each
    frequency. The util_avg value is extracted from scheduler trace events and
    its mean is compared with the expected value (ignoring the first 300ms so
    that the signal can stabilize). The test fails if the observed mean is
    beyond a certain error margin from the expected one. load_avg is then
    similarly compared with the expected util_avg mean, under the assumption
    that load_avg should equal util_avg when system load is light.

    Expected Behaviour
    ==================
    Load tracking signals are scaled so that the workload results in roughly the
    same util & load values regardless of frequency.
    """

    @classmethod
    def _getExperimentsConf(cls, test_env):
        # Run on one of the CPUs with highest capacity
        cpu = max(range(test_env.target.number_of_cpus),
                  key=lambda c: cls._get_cpu_capacity(test_env, c))

        wloads = {
            'fie_10pct' : cls.get_wload(cpu, 10)
        }

        # Create a set of confs with different frequencies
        # We'll run the 10% workload under each conf (i.e. at each frequency)
        confs = []

        all_freqs = test_env.target.cpufreq.list_frequencies(cpu)
        # If we have loads of frequencies just test a cross-section so it
        # doesn't take all day
        cls.freqs = all_freqs[::len(all_freqs)/8 + 1]
        for freq in cls.freqs:
            confs.append({
                'tag' : 'freq_{}'.format(freq),
                'flags' : ['ftrace', 'freeze_userspace'],
                'cpufreq' : {
                    'freqs' : {cpu: freq},
                    'governor' : 'userspace',
                },
            })

        return {
            'wloads': wloads,
            'confs': confs,
        }

    def _test_signal(self, experiment, tasks, signal_name):
        [task] = tasks
        exp_util = self.get_expected_util_avg(experiment)
        signal_mean = self.get_signal_mean(experiment, signal_name)

        error_margin = exp_util * (ERROR_MARGIN_PCT / 100.)
        [freq] = experiment.conf['cpufreq']['freqs'].values()

        msg = 'Saw {} around {}, expected {} at freq {}'.format(
            signal_name, signal_mean, exp_util, freq)
        self.assertAlmostEqual(signal_mean, exp_util, delta=error_margin,
                               msg=msg)

    @experiment_test
    def test_task_util_avg(self, experiment, tasks):
        """
        Test that the mean of the util_avg signal matched the expected value
        """
        return self._test_signal(experiment, tasks, 'util_avg')

    @experiment_test
    def test_task_load_avg(self, experiment, tasks):
        """
        Test that the mean of the load_avg signal matched the expected value

        Assuming that the system was under little stress (so the task was
        RUNNING whenever it was RUNNABLE) and that the task was run with a
        'nice' value of 0, the load_avg should be similar to the util_avg. So,
        this test does the same as test_task_util_avg but for load_avg.
        """
        return self._test_signal(experiment, tasks, 'load_avg')

class CpuInvarianceTest(_LoadTrackingBase):
    """
    Goal
    ====
    Basic check for CPU invariant load and utilization tracking

    Detailed Description
    ====================
    This test runs the same workload on one CPU of each type in the system. The
    trace is then examined to estimate an expected mean value for util_avg for
    each CPU's workload. The util_avg value is extracted from scheduler trace
    events and its mean is compared with the expected value (ignoring the first
    300ms so that the signal can stabilize). The test fails if the observed mean
    is beyond a certain error margin from the expected one. load_avg is then
    similarly compared with the expected util_avg mean, under the assumption
    that load_avg should equal util_avg when system load is light.

    Expected Behaviour
    ==================
    Load tracking signals are scaled so that the workload results in roughly the
    same util & load values regardless of compute power of the CPU
    used. Moreover, assuming that the extraneous system load is negligible, the
    load signal is similar to the utilization signal.
    """

    @classmethod
    def _getExperimentsConf(cls, test_env):
        # Run the 10% workload on one CPU in each capacity group
        wloads = {}
        tested_caps = set()
        for cpu in range(test_env.target.number_of_cpus):
            cap = cls._get_cpu_capacity(test_env, cpu)
            if cap in tested_caps:
                # No need to test on every CPU, just one for each capacity value
                continue
            tested_caps.add(cap)
            wloads['cie_cpu{}'.format(cpu)] = cls.get_wload(cpu, 10)

        conf = {
            'tag' : 'cie_conf',
            'flags' : ['ftrace', 'freeze_userspace'],
            'cpufreq' : {'governor' : 'performance'},
        }

        return {
            'wloads': wloads,
            'confs': [conf],
        }

    def _test_signal(self, experiment, tasks, signal_name):
        [task] = tasks
        exp_util = self.get_expected_util_avg(experiment)
        signal_mean = self.get_signal_mean(experiment, signal_name)

        error_margin = exp_util * (ERROR_MARGIN_PCT / 100.)
        [cpu] = experiment.wload.cpus

        msg = 'Saw {} around {}, expected {} on cpu {}'.format(
            signal_name, signal_mean, exp_util, cpu)
        self.assertAlmostEqual(signal_mean, exp_util, delta=error_margin,
                               msg=msg)

    @experiment_test
    def test_task_util_avg(self, experiment, tasks):
        """
        Test that the mean of the util_avg signal matched the expected value
        """
        return self._test_signal(experiment, tasks, 'util_avg')

class PELTTasksTest(_LoadTrackingBase):
    """
    Goal
    ====
    Basic checks for tasks related PELT signals behaviour.

    Detailed Description
    ====================
    This test runs a synthetic periodic task on a CPU in the system and
    collects a trace from the target device. The util_avg values are extracted
    from scheduler trace events and the behaviour of the signal is compared
    against a simulated value of PELT.
    This class runs the following tests:

    - test_util_avg_range: test that util_avg's stable range matches with the
        stable range of the simulated signal. In particular, this test compares
        min, max and mean values of the two signals.

    - test_util_avg_behaviour: check behaviour of util_avg against the simualted
        PELT signal. This test assumes that PELT is configured with 32 ms half
        life time and the samples are 1024 us. Also, it assumes that the first
        trace event related to the task used for testing is generated 'after'
        the task starts (hence, we compute the initial PELT value when the task
        started).

    Expected Behaviour
    ==================
    Simulated PELT signal and the signal extracted from the trace should have
    very similar min, max and mean values in the stable range and the behaviour of
    the signal should be very similar to simulated one.
    """

    @classmethod
    def _getExperimentsConf(cls, test_env):
        # Run the 50% workload on a CPU with highest capacity
        target_cpu = min(test_env.calibration(),
                         key=test_env.calibration().get)

        wloads = {
            'pelt_behv' : cls.get_wload(target_cpu, 50)
        }

        conf = {
            'tag' : 'pelt_behv_conf',
            'flags' : ['ftrace', 'freeze_userspace'],
            'cpufreq' : {'governor' : 'performance'},
        }

        return {
            'wloads': wloads,
            'confs': [conf],
        }

    def _test_range(self, experiment, tasks, signal_name):
        [task] = tasks
        signal_df = self.get_sched_task_signals(experiment, [signal_name])
        # Get stats and stable range of the simulated PELT signal
        start_time = self.get_task_start_time(experiment, task)
        init_pelt = pelt.Simulator.estimateInitialPeltValue(
            signal_df[signal_name].iloc[0], signal_df.index[0],
            start_time, HALF_LIFE_MS
        )
        peltsim, pelt_task, sim_df = self.get_simulated_pelt(experiment,
                                                             task,
                                                             init_pelt)
        sim_range = peltsim.stableRange(pelt_task)
        stable_time = peltsim.stableTime(pelt_task)
        window = (start_time + stable_time,
                  start_time + stable_time + 0.5)
        # Get signal statistics in a period of time where the signal is
        # supposed to be stable
        signal_stats = signal_df[window[0]:window[1]][signal_name].describe()

        # Narrow down simulated PELT signal to stable period
        sim_df = sim_df[window[0]:window[1]].pelt_value

        # Check min
        error_margin = sim_range.min_value * (ERROR_MARGIN_PCT / 100.)
        msg = 'Stable range min value around {}, expected {}'.format(
            signal_stats['min'], sim_range.min_value)
        self.assertAlmostEqual(sim_range.min_value, signal_stats['min'],
                               delta=error_margin, msg=msg)

        # Check max
        error_margin = sim_range.max_value * (ERROR_MARGIN_PCT / 100.)
        msg = 'Stable range max value around {}, expected {}'.format(
            signal_stats['max'], sim_range.max_value)
        self.assertAlmostEqual(sim_range.max_value, signal_stats['max'],
                               delta=error_margin, msg=msg)

        # Check mean
        sim_mean = sim_df.mean()
        error_margin = sim_mean * (ERROR_MARGIN_PCT / 100.)
        msg = 'Saw mean value of around {}, expected {}'.format(
            signal_stats['mean'], sim_mean)
        self.assertAlmostEqual(sim_mean, signal_stats['mean'],
                               delta=error_margin, msg=msg)

    def _test_behaviour(self, experiment, tasks, signal_name):
        [task] = tasks
        signal_df = self.get_sched_task_signals(experiment, [signal_name])
        # Get instant of time when the task starts running
        start_time = self.get_task_start_time(experiment, task)

        # Get information about the task
        phase = experiment.wload.params['profile'][task]['phases'][0]

        # Create simulated PELT signal for a periodic task
        init_pelt = pelt.Simulator.estimateInitialPeltValue(
            signal_df[signal_name].iloc[0], signal_df.index[0],
            start_time, HALF_LIFE_MS
        )
        peltsim, pelt_task, sim_df = self.get_simulated_pelt(
            experiment, task, init_pelt
        )

        # Compare actual PELT signal with the simulated one
        margin = 0.05
        period_s = phase.period_ms / 1e3
        sim_period_ms = phase.period_ms * (peltsim._sample_us / 1e6)
        n_errors = 0
        for entry in signal_df.iterrows():
            trace_val = entry[1][signal_name]
            timestamp = entry[0] - start_time
            # Next two instructions map the trace timestamp to a simulated
            # signal timestamp. This is due to the fact that the 1 ms is
            # actually 1024 us in the simulated signal.
            n_periods = timestamp / period_s
            nearest_timestamp = n_periods * sim_period_ms
            sim_val_loc = sim_df.index.get_loc(nearest_timestamp,
                                               method='nearest')
            sim_val = sim_df.pelt_value.iloc[sim_val_loc]
            if trace_val > (sim_val * (1 + margin)) or \
               trace_val < (sim_val * (1 - margin)):
                self._log.debug("At {} trace shows {}={}"
                                .format(entry[0], signal_name, trace_val))
                self._log.debug("At ({}, {}) simulation shows {}={}"
                                .format(sim_df.index[sim_val_loc],
                                        signal_name,
                                        sim_val_loc,
                                        sim_val))
                n_errors += 1

        msg = "Total number of errors: {}/{}".format(n_errors, len(signal_df))
        # Exclude possible outliers (these may be due to a kernel thread that
        # for some reason gets coscheduled with our workload).
        self.assertLess(n_errors/len(signal_df), margin, msg)

    @experiment_test
    def test_util_avg_range(self, experiment, tasks):
        """
        Test util_avg stable range for a 50% periodic task
        """
        return self._test_range(experiment, tasks, 'util_avg')

    @experiment_test
    def test_util_avg_behaviour(self, experiment, tasks):
        """
        Test util_avg behaviour for a 50% periodic task
        """
        return self._test_behaviour(experiment, tasks, 'util_avg')

    @experiment_test
    def test_load_avg_range(self, experiment, tasks):
        """
        Test load_avg stable range for a 50% periodic task
        """
        return self._test_range(experiment, tasks, 'load_avg')

    @experiment_test
    def test_load_avg_behaviour(self, experiment, tasks):
        """
        Test load_avg behaviour for a 50% periodic task
        """
        return self._test_behaviour(experiment, tasks, 'load_avg')

class _PELTTaskGroupsTest(LisaTest):
    """
    Abstract base class for generic tests on PELT taskgroups signals

    Subclasses should provide:
    - .tasks_conf member to generate the rt-app synthetics.
    - .target_cpu CPU where the tasks should run
    - .trace object where to save the parsed trace
    """
    test_conf = {
        'tools'    : [ 'rt-app' ],
        'ftrace' : {
            'events' : [
                'sched_switch',
                'sched_load_avg_task',
                'sched_load_avg_cpu',
                'sched_pelt_se',
                'sched_load_se',
                'sched_load_cfs_rq',
            ],
        },
        # cgroups required by freeze_userspace flag
        'modules': ['cpufreq', 'cgroups'],
    }
    # Allowed error margin
    allowed_util_margin = 0.02

    @classmethod
    def runExperiments(cls):
        """
        Set up logging and trigger running experiments
        """
        cls._log = logging.getLogger('LisaTest')

        cls._log.info('Setup tests execution engine...')
        te = TestEnv(test_conf=cls._getTestConf())

        experiments_conf = cls._getExperimentsConf(te)
        test_dir = os.path.join(te.res_dir, experiments_conf['confs'][0]['tag'])
        os.makedirs(test_dir)

        # Setting cpufreq governor to performance
        te.target.cpufreq.set_all_governors('performance')

        # Creating cgroups hierarchy
        cpuset_cnt = te.target.cgroups.controller('cpuset')
        cpu_cnt = te.target.cgroups.controller('cpu')

        max_duration = 0
        for se in cls.root_group.iter_nodes():
            if se.is_task:
                max_duration = max(max_duration, se.duration_s)

        # Freeze userspace tasks
        cls._log.info('Freezing userspace tasks')
        te.target.cgroups.freeze(Executor.critical_tasks[te.target.os])

        cls._log.info('FTrace events collection enabled')
        te.ftrace.start()

        # Run tasks
        cls._log.info('Running the tasks')
        # Run all tasks in background and wait for completion
        for se in cls.root_group.iter_nodes():
            if se.is_task:
                # Run tasks
                se.wload.run(out_dir=test_dir, cpus=se.cpus,
                             cgroup=se.parent.name, background=True)
        sleep(max_duration)

        te.ftrace.stop()

        trace_file = os.path.join(test_dir, 'trace.dat')
        te.ftrace.get_trace(trace_file)
        cls._log.info('Collected FTrace binary trace: %s', trace_file)

        # Un-freeze userspace tasks
        cls._log.info('Un-freezing userspace tasks')
        te.target.cgroups.freeze(thaw=True)

        # Extract trace
        cls.trace = Trace(None, test_dir, te.ftrace.events)

    def _test_group_util(self, group):
        if 'sched_load_se' not in self.trace.available_events:
            raise ValueError('No sched_load_se events. '
                             'Does the kernel support them?')
        if 'sched_load_cfs_rq' not in self.trace.available_events:
            raise ValueError('No sched_load_cfs_rq events. '
                             'Does the kernel support them?')
        if 'sched_switch' not in self.trace.available_events:
            raise ValueError('No sched_switch events. '
                             'Does the kernel support them?')

        task_util_df = self.trace.data_frame.trace_event('sched_load_se')
        tg_util_df = self.trace.data_frame.trace_event('sched_load_cfs_rq')
        sw_df = self.trace.data_frame.trace_event('sched_switch')

        tg = None
        for se in self.root_group.iter_nodes():
            if se.name == group:
                tg = se

        if tg is None:
            raise ValueError('{} taskgroup does not exist.'.format(group))

        # Only consider the time interval where the signal should be stable
        tasks_names = [se.name for se in tg.iter_nodes() if se.is_task]
        tasks_sw_df = sw_df[sw_df.next_comm.isin(tasks_names)]
        start = tasks_sw_df.index[0] + UTIL_AVG_CONVERGENCE_TIME
        end = tasks_sw_df.index[-1] - UTIL_AVG_CONVERGENCE_TIME
        task_util_df = task_util_df[start:end]
        tg_util_df = tg_util_df[start:end]

        # Compute mean util of the taskgroup and its children
        util_tg = tg_util_df[(tg_util_df.path == group) &
                             (tg_util_df.cpu == self.target_cpu)].util
        util_mean_tg = area_under_curve(util_tg) / (end - start)

        msg = 'Saw util {} for {} cgroup, expected {}'
        expected_trace_util = 0.0
        for child in tg.children:
            if child.is_task:
                util_s = task_util_df[task_util_df.comm == child.name].util
            else:
                util_s = tg_util_df[(tg_util_df.path == child.name) &
                                    (tg_util_df.cpu == self.target_cpu)].util

            util_mean = area_under_curve(util_s) / (end - start)
            # Make sure the trace utilization of children entities matches the
            # expected utilization (i.e. duty cycle for tasks, sum of utils for
            # taskgroups)
            expected = child.get_expected_util()
            error_margin = expected * (ERROR_MARGIN_PCT / 100.)
            self.assertAlmostEqual(util_mean, expected,
                                   delta=error_margin,
                                   msg=msg.format(util_mean,
                                                  child.name,
                                                  expected))

            expected_trace_util += util_mean

        error_margin = expected_trace_util * self.allowed_util_margin
        self.assertAlmostEqual(util_mean_tg, expected_trace_util,
                               delta=error_margin,
                               msg=msg.format(util_mean_tg,
                                              group,
                                              expected_trace_util))

class TwoGroupsCascade(_PELTTaskGroupsTest):
    """
    Test PELT utilization for task groups on the following hierarchy:

                           +-----+
                           | "/" |
                           +-----+
                           /     \
                          /       \
                      +------+   t0_1
                      |"/tg1"|
                      +------+
                       /     \
                      /       \
                    t1_1   +------------+
                           |"/tg1/tg1_1"|
                           +------------+
                              /      \
                             /        \
                           t2_1      t2_2

    """
    target_cpu = 0
    root_group = None
    trace = None

    @classmethod
    def _getExperimentsConf(cls, test_env):
        # Run all workloads on a CPU with highest capacity
        cls.target_cpu = min(test_env.calibration(),
                             key=test_env.calibration().get)

        # Create taskgroups
        cpus = test_env.target.list_online_cpus()
        mems = 0
        cls.root_group = Taskgroup("/", cpus, mems, test_env)
        tg1 = Taskgroup("/tg1", cpus, mems, test_env)
        tg1_1 = Taskgroup("/tg1/tg1_1", cpus, mems, test_env)

        # Create tasks
        period_ms = 16
        duty_cycle_pct = 10
        duration_s = 3
        cpus = [cls.target_cpu]
        t2_1 = Task("t2_1", test_env,
                    cpus, period_ms=period_ms,
                    duty_cycle_pct=duty_cycle_pct, duration_s=duration_s)
        t2_2 = Task("t2_2", test_env,
                    cpus, period_ms=period_ms,
                    duty_cycle_pct=duty_cycle_pct, duration_s=duration_s)
        t1_1 = Task("t1_1", test_env,
                    cpus, period_ms=period_ms,
                    duty_cycle_pct=duty_cycle_pct, duration_s=duration_s)
        t0_1 = Task("t0_1", test_env,
                    cpus, period_ms=period_ms,
                    duty_cycle_pct=duty_cycle_pct, duration_s=duration_s)

        # Link nodes to the hierarchy tree
        cls.root_group.add_children([t0_1, tg1])
        tg1.add_children([tg1_1, t1_1])
        tg1_1.add_children([t2_1, t2_2])

        conf = {
            'tag' : 'cgp_cascade',
            'flags' : ['ftrace', 'freeze_userspace'],
            'cpufreq' : {'governor' : 'performance'},
        }

        return {
            'wloads': {},
            'confs': [conf],
        }

    @classmethod
    def setUpClass(cls, *args, **kwargs):
        super(TwoGroupsCascade, cls).runExperiments(*args, **kwargs)

    def test_util_root_group(self):
        """
        Test utilization propagation to cgroup root
        """
        return self._test_group_util('/')

    def test_util_tg1_group(self):
        """
        Test utilization propagation to cgroup /tg1
        """
        return self._test_group_util('/tg1')

    def test_util_tg1_1_group(self):
        """
        Test utilization propagation to cgroup /tg1/tg1_1
        """
        return self._test_group_util('/tg1/tg1_1')

class UnbalancedHierarchy(_PELTTaskGroupsTest):
    """
    Test PELT utilization for task groups on the following hierarchy:

                                       +-----+
                                       | "/" |
                                       +-----+
                                       /     \
                                      /       \
                                  +------+   t0_1
                                  |"/tg1"|
                                  +------+
                                   /
                                  /
                           +----------+
                           |"/tg1/tg2"|
                           +----------+
                            /         \
                           /           \
                   +--------------+   t2_1
                   |"/tg1/tg2/tg3"|
                   +--------------+
                    /
                   /
           +------------------+
           |"/tg1/tg2/tg3/tg4"|
           +------------------+
            /
           /
        t4_1

    """
    target_cpu = 0
    root_group = None
    trace = None

    @classmethod
    def _getExperimentsConf(cls, test_env):
        # Run all workloads on a CPU with highest capacity
        cls.target_cpu = min(test_env.calibration(),
                             key=test_env.calibration().get)

        # Create taskgroups
        cpus = test_env.target.list_online_cpus()
        mems = 0
        cls.root_group = Taskgroup("/", cpus, mems, test_env)
        tg1 = Taskgroup("/tg1", cpus, mems, test_env)
        tg2 = Taskgroup("/tg1/tg2", cpus, mems, test_env)
        tg3 = Taskgroup("/tg1/tg2/tg3", cpus, mems, test_env)
        tg4 = Taskgroup("/tg1/tg2/tg3/tg4", cpus, mems, test_env)

        # Create tasks
        period_ms = 16
        duty_cycle_pct = 10
        duration_s = 3
        cpus = [cls.target_cpu]
        t0_1 = Task("t0_1", test_env,
                    cpus, period_ms=period_ms,
                    duty_cycle_pct=duty_cycle_pct, duration_s=duration_s)
        t2_1 = Task("t2_1", test_env,
                    cpus, period_ms=period_ms,
                    duty_cycle_pct=duty_cycle_pct, duration_s=duration_s)
        t4_1 = Task("t4_1", test_env,
                    cpus, period_ms=period_ms,
                    duty_cycle_pct=duty_cycle_pct, duration_s=duration_s)

        cls.root_group.add_children([t0_1, tg1])
        tg1.add_children([tg2])
        tg2.add_children([t2_1, tg3])
        tg3.add_children([tg4])
        tg4.add_children([t4_1])

        conf = {
            'tag' : 'cgp_unbalanced',
            'flags' : ['ftrace', 'freeze_userspace'],
            'cpufreq' : {'governor' : 'performance'},
        }

        return {
            'wloads': {},
            'confs': [conf],
        }

    @classmethod
    def setUpClass(cls, *args, **kwargs):
        super(UnbalancedHierarchy, cls).runExperiments(*args, **kwargs)

    def test_util_root_group(self):
        """
        Test utilization propagation to cgroup root
        """
        return self._test_group_util('/')

    def test_util_tg1_group(self):
        """
        Test utilization propagation to cgroup /tg1
        """
        return self._test_group_util('/tg1')

    def test_util_tg2_group(self):
        """
        Test utilization propagation to cgroup /tg1/tg2
        """
        return self._test_group_util('/tg1/tg2')

    def test_util_tg3_group(self):
        """
        Test utilization propagation to cgroup /tg1/tg2/tg3
        """
        return self._test_group_util('/tg1/tg2/tg3')

    def test_util_tg4_group(self):
        """
        Test utilization propagation to cgroup /tg1/tg2/tg3/tg4
        """
        return self._test_group_util('/tg1/tg2/tg3/tg4')

