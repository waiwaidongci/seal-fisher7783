"""表盘回卷模数还原求解器（纯标准库）。

业务口径
--------
两只机械表盘：总件表与漏封表。表盘为模数计数器，区间起止读数只保留末位，
回卷跨圈后直接相减会少算件数，真实区间产量 = 读数差 + 模数 × 圈数。

对区间 i：
  x_i = (te_i - ts_i) + mT_i * k_i          总件表区间产量，k_i >= 0
  y_i = (le_i - ls_i) + mL_i * l_i          漏封表区间产量，l_i >= 0
约束：
  0 <= y_i <= x_i <= U_i                    漏封不得超过总件，且受区间产量上限
全班实点：
  sum_i x_i = T（全班实点总件）
  sum_i y_i = L（全班实点漏封），L <= T
判定：
  1. 不存在满足全部约束的 (k_i, l_i) → 无解拒绝；
  2. 所有可行解的工艺级计数 (XA, XB, YA, YB) 必须完全相同，
     否则属于“计数歧义”拒绝（区间解不同、工艺计数相同则通过）；
  3. 计算超过时限（默认 5 秒）→ 超时拒绝。
"""
import time

# 状态/错误码（路由层与前端共用同一组字符串）
OK = 'ok'
NO_SOLUTION = 'no_solution'
AMBIGUOUS = 'ambiguous'
TIMEOUT = 'timeout'
INVALID = 'invalid'

PROCESS_A = 'A'
PROCESS_B = 'B'
PROCESSES = (PROCESS_A, PROCESS_B)


class SolverError(ValueError):
    """输入校验失败。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Deadline:
    def __init__(self, seconds):
        self.limit = float(seconds)
        self.deadline = time.perf_counter() + self.limit

    def check(self):
        if time.perf_counter() > self.deadline:
            raise SolverError(TIMEOUT, f'还原计算超过 {self.limit:.0f} 秒时限，已拒绝')


def _as_int(value, label):
    if isinstance(value, bool):
        raise SolverError(INVALID, f'{label}必须是整数')
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise SolverError(INVALID, f'{label}必须是整数')


def validate_shift(payload):
    """校验录入载荷，返回规范化后的 (intervals, actual_total, actual_missed)。"""
    if not isinstance(payload, dict):
        raise SolverError(INVALID, '请求体必须是对象')
    intervals = payload.get('intervals')
    if not isinstance(intervals, list) or not intervals:
        raise SolverError(INVALID, '至少录入一个区间')
    if len(intervals) > 200:
        raise SolverError(INVALID, '单个班次最多 200 个区间')
    clean = []
    for idx, raw in enumerate(intervals, 1):
        if not isinstance(raw, dict):
            raise SolverError(INVALID, f'区间 {idx} 格式不正确')
        process = raw.get('process')
        if process not in PROCESSES:
            raise SolverError(INVALID, f'区间 {idx} 的工艺必须为 A 或 B')
        mod_total = _as_int(raw.get('mod_total'), f'区间 {idx} 总件回卷模数')
        mod_missed = _as_int(raw.get('mod_missed'), f'区间 {idx} 漏封回卷模数')
        t_start = _as_int(raw.get('total_start'), f'区间 {idx} 总件起读数')
        t_end = _as_int(raw.get('total_end'), f'区间 {idx} 总件止读数')
        l_start = _as_int(raw.get('missed_start'), f'区间 {idx} 漏封起读数')
        l_end = _as_int(raw.get('missed_end'), f'区间 {idx} 漏封止读数')
        upper = _as_int(raw.get('upper'), f'区间 {idx} 区间产量上限')
        for name, val in (('总件回卷模数', mod_total), ('漏封回卷模数', mod_missed)):
            if val <= 0:
                raise SolverError(INVALID, f'区间 {idx} 的{name}必须为正整数')
        for name, val in (
            ('总件起读数', t_start), ('总件止读数', t_end),
            ('漏封起读数', l_start), ('漏封止读数', l_end),
        ):
            if not 0 <= val < (mod_total if name.startswith('总件') else mod_missed):
                raise SolverError(INVALID, f'区间 {idx} 的{name}必须在对应模数范围内')
        if upper < 0:
            raise SolverError(INVALID, f'区间 {idx} 的区间产量上限不得为负')
        clean.append({
            'index': idx, 'process': process,
            'mod_total': mod_total, 'mod_missed': mod_missed,
            'total_start': t_start, 'total_end': t_end,
            'missed_start': l_start, 'missed_end': l_end,
            'upper': upper,
        })
    actual_total = _as_int(payload.get('actual_total'), '全班实点总件')
    actual_missed = _as_int(payload.get('actual_missed'), '全班实点漏封')
    if actual_total < 0 or actual_missed < 0:
        raise SolverError(INVALID, '实点计数不得为负')
    if actual_missed > actual_total:
        raise SolverError(INVALID, '漏封数不得超过总件数')
    return clean, actual_total, actual_missed


def _candidates(interval, clock):
    """生成区间内全部合法 (x, y, k, l) 候选，按 x 再按 y 排序。"""
    dx = interval['total_end'] - interval['total_start']
    dy = interval['missed_end'] - interval['missed_start']
    upper = interval['upper']
    pairs = []
    k = 0
    while True:
        x = dx + interval['mod_total'] * k
        if x > upper:
            break
        if x < 0:
            k += 1
            continue
        l = 0
        while True:
            y = dy + interval['mod_missed'] * l
            if y > x:
                break
            if y >= 0:
                pairs.append((x, y, k, l))
                if len(pairs) % 10000 == 0:
                    clock.check()
            l += 1
        k += 1
        clock.check()
    if not pairs:
        raise SolverError(NO_SOLUTION, f"区间 {interval['index']} 不存在满足上限与漏封约束的候选计数")
    return dx, dy, pairs


def solve(payload, seconds=5.0, sample_limit=20, deadline_clock=None):
    """执行还原。

    返回 dict：
      status=ok 时含 counts（A/B 总件与漏封的唯一工艺计数）、
      intervals（每区间采用的 x/y/k/l 与读数依据）、
      samples（可行还原示例，区间解不同但工艺计数相同时可追溯）、
      feasible_solution_count（可行解数量，超过上限时标注 'many'）。
    """
    intervals, actual_total, actual_missed = validate_shift(payload)
    clock = deadline_clock or Deadline(seconds)

    prepared = []
    for interval in intervals:
        dx, dy, pairs = _candidates(interval, clock)
        prepared.append({**interval, 'dx': dx, 'dy': dy, 'pairs': pairs})
        clock.check()

    # 有界动态规划：状态 (累计总件 sx, 累计漏封 sy) -> 各工艺和的最小/最大值。
    # min==max（对全部可达状态成立）即可证明工艺级计数唯一。
    # 状态值：(min_ax, max_ax, min_ay, max_ay)
    dp = {(0, 0): (0, 0, 0, 0)}
    state_budget = max(200_000, (actual_total + 1) * (min(actual_missed, actual_total) + 1) * 2)
    for item in prepared:
        nxt = {}
        is_a = item['process'] == PROCESS_A
        for (sx, sy), (min_ax, max_ax, min_ay, max_ay) in dp.items():
            for x, y, _k, _l in item['pairs']:
                nx, ny = sx + x, sy + y
                if nx > actual_total or ny > actual_missed or ny > nx:
                    continue
                ax = x if is_a else 0
                ay = y if is_a else 0
                cand = (min_ax + ax, max_ax + ax, min_ay + ay, max_ay + ay)
                old = nxt.get((nx, ny))
                if old is None:
                    nxt[(nx, ny)] = cand
                else:
                    nxt[(nx, ny)] = (
                        min(old[0], cand[0]), max(old[1], cand[1]),
                        min(old[2], cand[2]), max(old[3], cand[3]),
                    )
        dp = nxt
        if not dp:
            raise SolverError(NO_SOLUTION, '不存在与全班实点相符的区间还原')
        if len(dp) > state_budget:
            raise SolverError(TIMEOUT, '中间状态数量超出时限预算，按计算超时拒绝')
        clock.check()

    target = (actual_total, actual_missed)
    if target not in dp:
        raise SolverError(NO_SOLUTION, '不存在与全班实点相符的区间还原')

    min_ax, max_ax, min_ay, max_ay = dp[target]
    if not (min_ax == max_ax and min_ay == max_ay):
        raise SolverError(
            AMBIGUOUS,
            '存在多组与实点相符的还原且工艺 A/B 计数不一致，属于计数歧义，已拒绝',
        )

    counts_a = {'total': max_ax, 'missed': max_ay}
    counts_b = {'total': actual_total - max_ax, 'missed': actual_missed - max_ay}
    if counts_b['total'] < 0 or counts_b['missed'] < 0:
        raise SolverError(AMBIGUOUS, '工艺计数推导出现矛盾，按歧义拒绝')

    samples = _collect_samples(prepared, actual_total, actual_missed, clock, sample_limit)
    chosen = samples[0]
    interval_basis = []
    for item, choice in zip(prepared, chosen):
        x, y, k, l = choice
        interval_basis.append({
            'index': item['index'], 'process': item['process'],
            'mod_total': item['mod_total'], 'mod_missed': item['mod_missed'],
            'total_start': item['total_start'], 'total_end': item['total_end'],
            'missed_start': item['missed_start'], 'missed_end': item['missed_end'],
            'upper': item['upper'],
            'total_delta': item['dx'], 'missed_delta': item['dy'],
            'total_turns': k, 'missed_turns': l,
            'total_count': x, 'missed_count': y,
            'formula_total': f"{item['total_end']}-{item['total_start']}+{item['mod_total']}×{k}={x}",
            'formula_missed': f"{item['missed_end']}-{item['missed_start']}+{item['mod_missed']}×{l}={y}",
        })

    solution_count = _count_solutions(prepared, actual_total, actual_missed, clock)
    return {
        'status': OK,
        'counts': {
            PROCESS_A: counts_a,
            PROCESS_B: counts_b,
        },
        'actual': {'total': actual_total, 'missed': actual_missed},
        'intervals': interval_basis,
        'samples': [
            [{'index': item['index'], 'total_count': c[0], 'missed_count': c[1],
              'total_turns': c[2], 'missed_turns': c[3]} for item, c in zip(prepared, sample)]
            for sample in samples
        ],
        'sample_truncated': len(samples) >= sample_limit,
        'feasible_solution_count': solution_count,
    }


def _collect_samples(prepared, actual_total, actual_missed, clock, sample_limit):
    """DFS 收集至多 sample_limit 个可行 (x,y,k,l) 组合，用于还原依据展示。"""
    samples = []

    def dfs(pos, sx, sy, chosen):
        if len(samples) >= sample_limit:
            return
        clock.check()
        if pos == len(prepared):
            if sx == actual_total and sy == actual_missed:
                samples.append(tuple(chosen))
            return
        for pair in prepared[pos]['pairs']:
            x, y = pair[0], pair[1]
            nx, ny = sx + x, sy + y
            if nx <= actual_total and ny <= actual_missed and ny <= nx:
                chosen.append(pair)
                dfs(pos + 1, nx, ny, chosen)
                chosen.pop()
            if len(samples) >= sample_limit:
                return

    dfs(0, 0, 0, [])
    if not samples:
        raise SolverError(NO_SOLUTION, '采样阶段未找到可行还原')
    return samples


def _count_solutions(prepared, actual_total, actual_missed, clock):
    """统计可行解总数；达到封顶数量时返回 'many'，避免计数随规模爆炸。"""
    cap = 10_000_000
    many = object()
    dp = {(0, 0): 1}

    def add(total, delta):
        if total is many or total + delta >= cap:
            return many
        return total + delta

    for item in prepared:
        nxt = {}
        for (sx, sy), cnt in dp.items():
            for x, y, _k, _l in item['pairs']:
                key = (sx + x, sy + y)
                if key[0] > actual_total or key[1] > actual_missed or key[1] > key[0]:
                    continue
                nxt[key] = add(nxt.get(key, 0), cnt)
        dp = nxt
        clock.check()
        if not dp:
            return 0
    value = dp.get((actual_total, actual_missed), 0)
    return 'many' if value is many else value
