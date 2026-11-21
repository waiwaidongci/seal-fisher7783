"""求解器单元测试，仅依赖标准库：python3 -m unittest backend.tests.test_rewind_solver -v

在 backend/ 目录下也可直接：python3 tests/test_rewind_solver.py
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services import rewind_solver as rs  # noqa: E402


def interval(process, mod_total, mod_missed, t_start, t_end, l_start, l_end, upper):
    return {
        'process': process,
        'mod_total': mod_total, 'mod_missed': mod_missed,
        'total_start': t_start, 'total_end': t_end,
        'missed_start': l_start, 'missed_end': l_end,
        'upper': upper,
    }


class TestCandidates(unittest.TestCase):
    def test_rewind_basic_single_interval(self):
        # 模数 100：起 90 止 10，读数差 -80，回卷一圈 → 产量 20；漏封同理 5
        payload = {
            'intervals': [interval('A', 100, 50, 90, 10, 45, 0, 100)],
            'actual_total': 20, 'actual_missed': 5,
        }
        out = rs.solve(payload)
        self.assertEqual(out['status'], rs.OK)
        self.assertEqual(out['counts']['A'], {'total': 20, 'missed': 5})
        self.assertEqual(out['counts']['B'], {'total': 0, 'missed': 0})
        self.assertEqual(out['intervals'][0]['total_turns'], 1)
        self.assertEqual(out['intervals'][0]['missed_turns'], 1)

    def test_direct_subtract_would_undercount(self):
        # 直接相减得 -80，必须还原为 20；若按实点错误录入 -80 则无解
        payload = {
            'intervals': [interval('A', 100, 50, 90, 10, 45, 0, 100)],
            'actual_total': -80, 'actual_missed': 5,
        }
        with self.assertRaises(rs.SolverError) as ctx:
            rs.solve(payload)
        self.assertEqual(ctx.exception.code, rs.INVALID)

    def test_missed_exceeds_total_rejected(self):
        payload = {
            'intervals': [interval('A', 100, 100, 0, 10, 0, 20, 100)],
            'actual_total': 10, 'actual_missed': 11,
        }
        with self.assertRaises(rs.SolverError) as ctx:
            rs.solve(payload)
        self.assertEqual(ctx.exception.code, rs.INVALID)

    def test_no_solution_when_actual_unreachable(self):
        payload = {
            'intervals': [interval('A', 100, 100, 0, 10, 0, 2, 100)],
            'actual_total': 999, 'actual_missed': 0,
        }
        with self.assertRaises(rs.SolverError) as ctx:
            rs.solve(payload)
        self.assertEqual(ctx.exception.code, rs.NO_SOLUTION)

    def test_upper_bound_cuts_candidates(self):
        # 候选 x = 10, 110, ...；上限 50 只留 10
        payload = {
            'intervals': [interval('A', 100, 100, 0, 10, 0, 1, 50)],
            'actual_total': 10, 'actual_missed': 1,
        }
        out = rs.solve(payload)
        self.assertEqual(out['counts']['A']['total'], 10)

    def test_interval_candidate_empty_no_solution(self):
        # 漏封最小候选 y=5，但总件最大 x=2（上限 2），y>x 全部被剔除
        payload = {
            'intervals': [interval('A', 100, 100, 0, 2, 0, 5, 10)],
            'actual_total': 2, 'actual_missed': 0,
        }
        with self.assertRaises(rs.SolverError) as ctx:
            rs.solve(payload)
        self.assertEqual(ctx.exception.code, rs.NO_SOLUTION)


class TestAmbiguity(unittest.TestCase):
    def test_ambiguous_process_counts_rejected(self):
        # 两个 A 区间，模数 10，dx=0，上限 20 → x ∈ {0,10,20}；y 固定为 0。
        # 实点总件 20：可 (0,20)/(10,10)/(20,0)，A 总件恒 20，需要 B 才能产生歧义。
        # 改为一个 A 一个 B，实点 20：A 总件可取 0/10/20 → 工艺计数歧义。
        payload = {
            'intervals': [
                interval('A', 10, 10, 0, 0, 0, 0, 20),
                interval('B', 10, 10, 0, 0, 0, 0, 20),
            ],
            'actual_total': 20, 'actual_missed': 0,
        }
        with self.assertRaises(rs.SolverError) as ctx:
            rs.solve(payload)
        self.assertEqual(ctx.exception.code, rs.AMBIGUOUS)

    def test_different_interval_solutions_same_process_counts_accepted(self):
        # 关键验收：区间解不同但工艺计数相同，必须通过。
        # 两个 A 区间（同工艺），模数 10，dx=0，上限 20，y=0；实点 20。
        # 可行解 (0,20)/(10,10)/(20,0) 区间分配不同，但 A 总件恒为 20、漏封恒 0。
        payload = {
            'intervals': [
                interval('A', 10, 10, 0, 0, 0, 0, 20),
                interval('A', 10, 10, 0, 0, 0, 0, 20),
            ],
            'actual_total': 20, 'actual_missed': 0,
        }
        out = rs.solve(payload)
        self.assertEqual(out['status'], rs.OK)
        self.assertEqual(out['counts']['A'], {'total': 20, 'missed': 0})
        self.assertEqual(out['counts']['B'], {'total': 0, 'missed': 0})
        self.assertGreaterEqual(len(out['samples']), 2)
        self.assertEqual(out['feasible_solution_count'], 3)
        # 采样之间至少有一组区间分配不同
        self.assertNotEqual(
            [(s['total_count'], s['missed_count']) for s in out['samples'][0]],
            [(s['total_count'], s['missed_count']) for s in out['samples'][1]],
        )

    def test_missed_side_ambiguity_also_rejected(self):
        # 总件侧唯一，漏封侧在 A/B 间可转移 → 歧义
        payload = {
            'intervals': [
                interval('A', 100, 10, 0, 10, 0, 0, 10),
                interval('B', 100, 10, 0, 10, 0, 0, 10),
            ],
            'actual_total': 20, 'actual_missed': 10,
        }
        # A: x=10, y∈{0,10}; B 同。漏封和为 10 时 A 漏封可取 0 或 10 → 歧义
        with self.assertRaises(rs.SolverError) as ctx:
            rs.solve(payload)
        self.assertEqual(ctx.exception.code, rs.AMBIGUOUS)

    def test_multi_turn_span(self):
        # 区间可跨多圈：A 表 90→5（差值 -85，模数 100），上限 150 保留 x∈{15,115}；
        # B 表零圈区间，上限 50 只保留 (10,2)，工艺拆分唯一。
        # 实点 125/17 唯一锁定 A=(115,15)（总件 2 圈、漏封 1 圈）、B=(10,2)（零圈）。
        payload = {
            'intervals': [
                interval('A', 100, 100, 90, 5, 90, 5, 150),
                interval('B', 100, 100, 0, 10, 0, 2, 50),
            ],
            'actual_total': 125, 'actual_missed': 17,
        }
        out = rs.solve(payload)
        self.assertEqual(out['counts']['A']['total'], 115)
        self.assertEqual(out['counts']['A']['missed'], 15)
        self.assertEqual(out['counts']['B']['total'], 10)
        self.assertEqual(out['counts']['B']['missed'], 2)
        self.assertEqual(out['intervals'][0]['total_turns'], 2)
        self.assertEqual(out['intervals'][0]['missed_turns'], 1)
        self.assertEqual(out['intervals'][1]['total_turns'], 0)
        self.assertEqual(out['intervals'][1]['missed_turns'], 0)
        self.assertEqual(out['feasible_solution_count'], 1)


class TestTimeout(unittest.TestCase):
    class FakeClock:
        limit = 5.0

        def __init__(self, fail_after_checks):
            self.checks = 0
            self.fail_after_checks = fail_after_checks

        def check(self):
            self.checks += 1
            if self.checks >= self.fail_after_checks:
                raise rs.SolverError(rs.TIMEOUT, '还原计算超过 5 秒时限，已拒绝')

    def test_timeout_raises_in_candidate_phase(self):
        # 注入时钟：候选生成阶段即触发时限，确保按 timeout 拒绝而不是无解
        payload = {
            'intervals': [
                interval('A', 1000, 1000, 0, 1, 0, 0, 5000) for _ in range(3)
            ],
            'actual_total': 3000, 'actual_missed': 0,
        }
        with self.assertRaises(rs.SolverError) as ctx:
            rs.solve(payload, deadline_clock=self.FakeClock(fail_after_checks=1))
        self.assertEqual(ctx.exception.code, rs.TIMEOUT)

    def test_timeout_raises_in_dp_phase(self):
        payload = {
            'intervals': [
                interval('A', 10, 10, 0, 0, 0, 0, 20),
                interval('B', 10, 10, 0, 0, 0, 0, 20),
            ],
            'actual_total': 20, 'actual_missed': 0,
        }
        with self.assertRaises(rs.SolverError) as ctx:
            # 前 2 次检查（准备阶段两个区间）通过，动态规划阶段触发
            rs.solve(payload, deadline_clock=self.FakeClock(fail_after_checks=4))
        self.assertEqual(ctx.exception.code, rs.TIMEOUT)

    def test_large_case_finishes_quickly(self):
        # 50 个区间、大模数，5 秒内应完成且结果确定
        payload = {
            'intervals': [
                interval('A' if i % 2 else 'B', 10000, 10000,
                         (i * 7) % 10000, (i * 7 + 3) % 10000,
                         (i * 3) % 10000, (i * 3 + 1) % 10000,
                         1_000_000)
                for i in range(50)
            ],
            # 每个区间最小候选：总件差 3、漏封差 1，50 个区间全取零圈解
            'actual_total': 50 * 3, 'actual_missed': 50 * 1,
        }
        start = time.perf_counter()
        out = rs.solve(payload, seconds=5.0)
        self.assertLess(time.perf_counter() - start, 5.0)
        self.assertEqual(out['status'], rs.OK)
        self.assertEqual(out['counts']['A']['total'], 25 * 3)
        self.assertEqual(out['counts']['B']['total'], 25 * 3)
        self.assertEqual(out['counts']['A']['missed'], 25)
        self.assertEqual(out['counts']['B']['missed'], 25)


class TestValidation(unittest.TestCase):
    def _base(self):
        return {
            'intervals': [interval('A', 100, 100, 0, 10, 0, 1, 100)],
            'actual_total': 10, 'actual_missed': 1,
        }

    def test_bad_process(self):
        p = self._base(); p['intervals'][0]['process'] = 'C'
        with self.assertRaisesRegex(rs.SolverError, '工艺'):
            rs.solve(p)

    def test_reading_out_of_range(self):
        p = self._base(); p['intervals'][0]['total_end'] = 100
        with self.assertRaisesRegex(rs.SolverError, '模数范围'):
            rs.solve(p)

    def test_mod_must_positive(self):
        p = self._base(); p['intervals'][0]['mod_total'] = 0
        with self.assertRaisesRegex(rs.SolverError, '模数'):
            rs.solve(p)

    def test_empty_intervals(self):
        p = self._base(); p['intervals'] = []
        with self.assertRaises(rs.SolverError) as ctx:
            rs.solve(p)
        self.assertEqual(ctx.exception.code, rs.INVALID)

    def test_bool_not_int(self):
        p = self._base(); p['actual_total'] = True
        with self.assertRaises(rs.SolverError) as ctx:
            rs.solve(p)
        self.assertEqual(ctx.exception.code, rs.INVALID)


def _brute_force(payload):
    """独立穷举 oracle：枚举每区间候选的笛卡尔积，返回状态与工艺计数集合。"""
    import itertools
    intervals, actual_total, actual_missed = rs.validate_shift(payload)
    all_pairs = []
    for iv in intervals:
        _dx, _dy, pairs = rs._candidates(iv, rs.Deadline(5))
        all_pairs.append(pairs)
    process_counts = set()
    for combo in itertools.product(*all_pairs):
        tx = sum(c[0] for c in combo)
        ly = sum(c[1] for c in combo)
        if tx != actual_total or ly != actual_missed:
            continue
        ax = sum(c[0] for iv, c in zip(intervals, combo) if iv['process'] == 'A')
        ay = sum(c[1] for iv, c in zip(intervals, combo) if iv['process'] == 'A')
        process_counts.add((ax, ay, actual_total - ax, actual_missed - ay))
    if not process_counts:
        return rs.NO_SOLUTION, set()
    if len(process_counts) > 1:
        return rs.AMBIGUOUS, process_counts
    return rs.OK, process_counts


class TestOracle(unittest.TestCase):
    def test_random_small_instances_match_brute_force(self):
        import random
        rng = random.Random(20261120)
        clock = rs.Deadline(5)
        for case_no in range(120):
            n = rng.randint(1, 3)
            intervals = []
            clean = []
            while len(intervals) < n:
                mod_t = rng.choice((5, 10, 20))
                mod_l = rng.choice((5, 10, 20))
                iv = {
                    'index': len(intervals) + 1,
                    'process': rng.choice(('A', 'B')),
                    'mod_total': mod_t, 'mod_missed': mod_l,
                    'total_start': rng.randrange(mod_t), 'total_end': rng.randrange(mod_t),
                    'missed_start': rng.randrange(mod_l), 'missed_end': rng.randrange(mod_l),
                    'upper': rng.randint(0, 40),
                }
                try:
                    rs._candidates(iv, clock)
                except rs.SolverError:
                    continue  # 跳过无候选区间，无解判定由专门用例覆盖
                intervals.append({k: iv[k] for k in (
                    'process', 'mod_total', 'mod_missed', 'total_start', 'total_end',
                    'missed_start', 'missed_end', 'upper')})
                clean.append(iv)
            # 实点在可达范围内随机：先取一个随机组合再加噪声
            total = missed = 0
            for iv in clean:
                _dx, _dy, pairs = rs._candidates(iv, clock)
                x, y, _k, _l = rng.choice(pairs)
                total += x; missed += y
            if rng.random() < 0.3:
                total = max(0, total + rng.randint(-8, 8))
                missed = min(total, max(0, missed + rng.randint(-4, 4)))
            payload = {'intervals': intervals, 'actual_total': total, 'actual_missed': missed}
            expected, counts = _brute_force(payload)
            try:
                out = rs.solve(payload)
                got = rs.OK
            except rs.SolverError as exc:
                got = exc.code
            self.assertEqual(got, expected, f'case {case_no}: {payload}')
            if got == rs.OK:
                (ax, ay, bx, by) = next(iter(counts))
                self.assertEqual(out['counts']['A'], {'total': ax, 'missed': ay})
                self.assertEqual(out['counts']['B'], {'total': bx, 'missed': by})
                # 输出内部自洽
                self.assertEqual(ax + bx, total)
                self.assertEqual(ay + by, missed)
                for basis in out['intervals']:
                    self.assertLessEqual(basis['missed_count'], basis['total_count'])
                    self.assertLessEqual(basis['total_count'], basis['upper'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
