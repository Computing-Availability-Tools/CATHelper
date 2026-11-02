FilePath = ""
OutputPath = ""
ZP_BUBBLE_ABNORMAL_BOUNDARY = 50000
COMPUTE_THRESHOLD = 1.3
IO_THRESHOLD = 2.5
COMM_THRESHOLD = 1.3
BUBBLE_THRESHOLD_NS = 5000
MAX_K = 10
MAX_ITERATIONS = 300
RECURSION_DEPTH = 10
CONVERGENCE_EPS = 1e-9
SLOW_COMM_MIN_COUNT = 1000
SLOW_COMM_COUNT_FLOOR = 10240
COMM_NO_ALIGN = False

def set_comm_no_align(flag: bool):
    global COMM_NO_ALIGN
    COMM_NO_ALIGN = bool(flag)

def get_comm_no_align() -> bool:
    return COMM_NO_ALIGN
CommTwoLevelDetail = None

def set_comm_two_level_detail(detail):
    global CommTwoLevelDetail
    CommTwoLevelDetail = detail

def get_comm_two_level_detail():
    return CommTwoLevelDetail
PP_WAIT_GAP_COLUMN = "PP_WaitGap"
PP_WAIT_RATIO_THRESHOLD = 0.05
PP_SESSION_NS = 0

def set_pp_session_ns(ns):
    global PP_SESSION_NS
    try:
        PP_SESSION_NS = int(ns) if ns and int(ns) > 0 else 0
    except (TypeError, ValueError):
        PP_SESSION_NS = 0

def get_pp_session_ns() -> int:
    return PP_SESSION_NS
IsClusterData = False
HasNamedDomain = False
HostRankMap = {}
RankDeviceMap = {}
JobType = "unknown"
ProfilingLevel = "levelnone"

def set_host_rank_map(rank: int, host_name):
    HostRankMap[str(rank)] = host_name

def get_host_rank_map() -> dict:
    return HostRankMap

def reset_host_rank_map():
    HostRankMap.clear()

def set_rank_device_map(rank, host_name, npu_id):
    RankDeviceMap[str(rank)] = {"host_name": host_name, "npu_id": npu_id}

def get_rank_device_map() -> dict:
    return RankDeviceMap

def reset_rank_device_map():
    RankDeviceMap.clear()

def get_node_name(rank) -> str:
    info = RankDeviceMap.get(str(rank))
    if info and info.get("host_name"):
        return info["host_name"]
    uid = HostRankMap.get(str(rank))
    if uid:
        return uid
    return f"rank{rank}"

def get_node_ranks_map() -> dict:
    ranks = set(HostRankMap.keys()) | set(RankDeviceMap.keys())
    m = {}
    for r in ranks:
        try:
            m.setdefault(get_node_name(int(r)), []).append(int(r))
        except (TypeError, ValueError):
            continue
    return {k: sorted(v) for k, v in m.items()}

def set_job_type(job_type: str):
    global JobType
    JobType = job_type

def get_job_type() -> str:
    return JobType

def set_profiling_level(level: str):
    global ProfilingLevel
    ProfilingLevel = level

def get_profiling_level() -> str:
    return ProfilingLevel

def set_is_cluster_data(is_cluster: bool):
    global IsClusterData
    IsClusterData = is_cluster

def get_is_cluster_data() -> bool:
    return IsClusterData

def set_has_named_domain(v: bool):
    global HasNamedDomain
    HasNamedDomain = v

def get_has_named_domain() -> bool:
    return HasNamedDomain

class DegradationData(dict):
    def __init__(self):
        super().__init__()
    @staticmethod
    def _single_key(rank: int, domain: str = None) -> str:
        return f"{domain}:{rank}" if domain else str(rank)
    @staticmethod
    def _group_key(ranks: list) -> str:
        if not ranks:
            return ""
        sorted_ranks = sorted(ranks)
        return ",".join(str(r) for r in sorted_ranks)
    def add_single(self, category: str, rank: int, degradation: float, domain: str = None):
        if category not in self:
            self[category] = {}
        key = self._single_key(rank, domain)
        self[category][key] = degradation
    def add_group(self, category: str, ranks: list, degradation: float):
        if not ranks:
            return
        if category not in self:
            self[category] = {}
        key = self._group_key(ranks)
        if key in self[category]:
            self[category][key] = max(self[category][key], degradation)
        else:
            self[category][key] = degradation

def set_file_path(path: str):
    global FilePath
    FilePath = path

def get_file_path() -> str:
    return FilePath

def set_output_path(path: str):
    global OutputPath
    OutputPath = path

def get_output_path() -> str:
    return OutputPath if OutputPath else FilePath

def set_thresholds(compute: float = 1.3, io: float = 2.5, comm: float = 1.3):
    global COMPUTE_THRESHOLD, IO_THRESHOLD, COMM_THRESHOLD
    COMPUTE_THRESHOLD = compute
    IO_THRESHOLD = io
    COMM_THRESHOLD = comm

def get_compute_threshold() -> float:
    return COMPUTE_THRESHOLD

def get_io_threshold() -> float:
    return IO_THRESHOLD

def get_comm_threshold() -> float:
    return COMM_THRESHOLD
COMPUTE_CATEGORIES = ("KERNEL_AICORE", "kernel_aivec")
IO_CATEGORIES = ("cpu", "memcpy_async")
COMM_CATEGORIES = ("comm", "pp_comm")

def get_threshold_for_category(category: str) -> float:
    if category in IO_CATEGORIES:
        return IO_THRESHOLD
    if category in COMM_CATEGORIES:
        return COMM_THRESHOLD
    return COMPUTE_THRESHOLD
