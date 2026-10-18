// 한국어. Keys are the English texts used in the code; a missing key shows the English (i18n.js).
export default {
  // ---- shell, navigation ----
  "Dashboard": "대시보드",
  "Workflow": "워크플로",
  "Queue": "대기열",
  "History": "기록",
  "System": "시스템",
  "Log out": "로그아웃",
  "ComfyUI on its own, in a new tab": "ComfyUI만 새 탭에서 열기",
  "{user} (admin)": "{user} (관리자)",
  "Cannot load data ({error}).": "데이터를 불러올 수 없습니다 ({error}).",
  "Showing data from {time}.": "{time} 기준 데이터를 표시합니다.",
  "updated {time}": "{time} 갱신",
  "{running} running · {queued} waiting": "실행 {running} · 대기 {queued}",
  "Administrators only.": "관리자만 볼 수 있습니다.",
  "cannot reach the server ({error})": "서버에 연결할 수 없습니다 ({error})",
  "not logged in": "로그인되어 있지 않습니다",

  // ---- units, time ----
  "s": "초", "m": "분", "h": "시간", "d": "일",
  "{time} ago": "{time} 전",
  "24 h": "24시간", "7 days": "7일", "30 days": "30일",
  "Apply": "적용",
  "pick both dates": "시작일과 종료일을 모두 고르세요",
  "end is before start": "종료일이 시작일보다 앞섭니다",
  "at most {n} days": "최대 {n}일까지",
  "max {n} days": "최대 {n}일",

  // ---- job and worker states (pills) ----
  "pending": "대기", "waiting": "대기", "paused": "일시정지", "dispatching": "전달 중", "running": "실행 중",
  "cancelling": "취소 중", "completed": "완료", "failed": "실패", "cancelled": "취소됨", "blocked": "실행 불가",
  "online": "온라인", "offline": "오프라인", "disabled": "비활성", "maintenance": "점검", "active": "사용 중",
  "unknown": "알 수 없음",

  // ---- common ----
  "Save": "저장", "Saved": "저장했습니다", "Cancel": "취소", "Close": "닫기", "Create": "만들기", "Add": "추가",
  "Edit": "편집", "Delete": "삭제", "Deleted": "삭제했습니다", "Refresh": "새로 고침", "Enable": "사용", "Disable": "사용 안 함",
  "Edit {name}": "{name} 편집",
  "none available": "선택할 수 있는 항목이 없습니다",
  "note": "메모", "Note": "메모", "Note saved": "메모를 저장했습니다",

  // ---- job tables ----
  "Status": "상태", "Owner": "소유자", "Group": "그룹", "Worker": "워커", "Progress": "진행",
  "Waiting / running": "대기 / 실행 시간", "Finished": "종료", "Run time": "실행 시간", "Error": "오류",
  "(any)": "(아무 그룹)",
  "Run next": "다음 차례로", "Move up": "위로", "Move down": "아래로", "Move to the end": "맨 뒤로",
  "Pause": "일시정지", "Resume": "재개", "Keep it in the queue but skip it": "대기열에 두되 건너뜁니다",
  "Interrupt this running job?": "실행 중인 작업을 중단할까요?",
  "No worker in a group you may use can run this job (requirements or permissions).":
    "사용할 수 있는 그룹에 이 작업을 실행할 워커가 없습니다 (요구 조건 또는 권한).",
  "Retry": "다시 실행", "Queue a new job with the same workflow": "같은 워크플로로 새 작업을 대기열에 넣습니다",
  "Queued again as {id}": "{id}(으)로 다시 넣었습니다",
  "job {id}": "작업 {id}",

  // ---- group cards ----
  "You may not use any worker group yet. Ask an administrator.": "아직 사용할 수 있는 워커 그룹이 없습니다. 관리자에게 요청하세요.",
  "{online}/{workers} online": "온라인 {online}/{workers}",
  "Idle workers": "유휴 워커", "Waiting": "대기", "Running": "실행 중", "Paused": "일시정지",
  "Dispatch stalled": "배정 멈춤", "Longest running": "최장 실행", "Cannot run": "실행 불가", "Consistency": "구성 일치",
  "{n} job(s)": "{n}건", "{n} worker(s) differ": "워커 {n}대가 다름",

  // ---- dashboard ----
  "My jobs": "내 작업", "My groups": "내 그룹", "My usage": "내 사용량", "Recent": "최근", "All history →": "전체 기록 →",
  "Nothing queued. Submit from ComfyUI.": "대기 중인 작업이 없습니다. ComfyUI에서 실행하세요.",
  "No finished jobs yet.": "아직 끝난 작업이 없습니다.",
  "jobs finished": "끝난 작업", "GPU time": "GPU 시간", "avg wait": "평균 대기", "avg run": "평균 실행",
  "{date}: {completed} done, {failed} failed, {cancelled} cancelled": "{date}: 완료 {completed}, 실패 {failed}, 취소 {cancelled}",

  // ---- queue / history ----
  "all unfinished": "끝나지 않은 전체", "all": "전체", "all groups": "모든 그룹", "all users": "모든 사용자",
  "No matching jobs.": "조건에 맞는 작업이 없습니다.",
  "search id or note": "ID 또는 메모 검색", "search id, note, owner": "ID, 메모, 소유자 검색",
  "search id, note or error": "ID, 메모, 오류 검색", "search id, note, error, owner": "ID, 메모, 오류, 소유자 검색",
  "farm: {queued} waiting · {running} running": "팜 전체: 대기 {queued} · 실행 {running}",
  "⤒ run next · ↑↓ move past the neighbour shown · ⤓ to the end. One order is shared by all groups.":
    "⤒ 다음 차례로 · ↑↓ 보이는 이웃과 순서 바꾸기 · ⤓ 맨 뒤로. 순서는 모든 그룹이 함께 씁니다.",
  "← Newer": "← 최근", "Older →": "이전 →", "{from}–{to} of {total}": "{total}건 중 {from}–{to}",

  // ---- personal settings ----
  "Appearance": "화면", "Light": "라이트", "Dark": "다크", "Auto (follow the system)": "자동 (시스템 설정 따름)",
  "Theme saved": "테마를 저장했습니다",
  "Language": "언어", "Auto (browser language)": "자동 (브라우저 언어)",
  "Password": "비밀번호", "Current password": "현재 비밀번호", "New password": "새 비밀번호",
  "New password again": "새 비밀번호 확인", "Change password": "비밀번호 변경",
  "at least 8 characters": "8자 이상", "the new passwords differ": "새 비밀번호가 서로 다릅니다",
  "Changing it signs this account out everywhere, this browser included.": "바꾸면 이 브라우저를 포함해 모든 곳에서 로그아웃됩니다.",
  "This account signs in through your organisation's login; change the password there.":
    "이 계정은 조직의 로그인으로 들어옵니다. 비밀번호는 그곳에서 바꾸세요.",
  "Worker groups you may use": "사용할 수 있는 워커 그룹",
  "All groups (administrator).": "모든 그룹 (관리자).", "All groups.": "모든 그룹.",
  "None yet — ask an administrator.": "아직 없습니다 — 관리자에게 요청하세요.",

  // ---- system: tabs ----
  "Workers": "워커", "Groups": "그룹", "Users": "사용자", "Rollout": "롤아웃", "Statistics": "통계", "Settings": "설정",

  // ---- system: workers ----
  "Name": "이름", "State": "상태", "GPU": "GPU", "Job": "작업", "Warnings": "경고",
  "(no group)": "(그룹 없음)", "Add worker": "워커 추가", "ComfyUI URL": "ComfyUI 주소",
  "Worker added — it appears online after its first health check": "워커를 추가했습니다 — 첫 상태 점검 뒤 온라인으로 보입니다",
  "Maintenance: no new jobs, the running one finishes. Disable: also no health checks. Delete keeps the worker's history.":
    "점검: 새 작업을 받지 않고 실행 중인 작업은 끝까지 갑니다. 사용 안 함: 상태 점검도 하지 않습니다. 삭제해도 워커의 기록은 남습니다.",
  "Group changed": "그룹을 바꿨습니다",
  "Disable {name}? It stops receiving jobs and is no longer health-checked.": "{name}을(를) 사용 안 함으로 둘까요? 작업을 받지 않고 상태 점검도 멈춥니다.",
  "changing it re-reads the worker's capabilities": "바꾸면 워커의 능력 정보를 다시 읽습니다",
  "Delete {name}?": "{name}을(를) 삭제할까요?",
  "Delete {name}? Its running job will be reported as lost.": "{name}을(를) 삭제할까요? 실행 중인 작업은 유실로 처리됩니다.",
  "idle": "유휴", "End maintenance": "점검 끝내기", "Maintenance": "점검",

  // ---- system: groups ----
  "Runs on": "실행 조건", "Unfinished jobs": "끝나지 않은 작업", "Node list": "노드 목록",
  "New group": "새 그룹", "New worker group": "새 워커 그룹", "Group created": "그룹을 만들었습니다",
  "Refresh nodes": "노드 목록 새로 고침", "Node list refreshed": "노드 목록을 새로 읽었습니다",
  "Delete group {name}?": "{name} 그룹을 삭제할까요?",
  "fetched {time}": "{time} 읽음", "not fetched yet": "아직 읽지 않음",
  "A group can only be deleted when it has no workers and no unfinished jobs. Refresh the node list after installing or updating custom nodes on its workers.":
    "워커와 끝나지 않은 작업이 없는 그룹만 삭제할 수 있습니다. 워커에 커스텀 노드를 설치하거나 업데이트했다면 노드 목록을 새로 고치세요.",
  "any worker": "모든 워커", "Linux/macOS": "Linux/macOS", "Linux / macOS": "Linux / macOS", "Windows": "Windows",
  "≥ {n} GB VRAM": "VRAM {n} GB 이상",
  "Backend (none checked = any)": "백엔드 (선택 없음 = 모두)",
  "Operating system (none checked = any)": "운영체제 (선택 없음 = 모두)",
  "Minimum VRAM (GB)": "최소 VRAM (GB)",

  // ---- system: users ----
  "User": "사용자", "Role": "역할", "Sign-in": "로그인 방식", "Worker groups": "워커 그룹", "Last job": "마지막 작업",
  "Worker groups this user may use": "이 사용자가 쓸 수 있는 워커 그룹",
  "administrators may use every group regardless": "관리자는 설정과 관계없이 모든 그룹을 씁니다",
  "New user": "새 사용자", "User name": "사용자 이름", "Initial password": "초기 비밀번호", "administrator": "관리자",
  "User created": "사용자를 만들었습니다",
  "leave empty to keep the current one": "비워 두면 지금 비밀번호를 유지합니다",
  "this account signs in through another provider": "이 계정은 다른 인증 수단으로 로그인합니다",
  "admin": "관리자", "user": "사용자", "password": "비밀번호", "external": "외부",
  "all (admin)": "전체 (관리자)", "none — cannot submit": "없음 — 작업을 낼 수 없음",
  "Disable {username}? They are signed out and cannot log in.": "{username}을(를) 사용 안 함으로 둘까요? 로그아웃되고 다시 로그인할 수 없습니다.",
  "Delete the account {name}? Their jobs and history stay.": "{name} 계정을 삭제할까요? 작업과 기록은 남습니다.",
  "Disabling a user or setting a new password signs them out within 30 seconds. Their jobs and history stay. The last active administrator cannot be demoted, disabled or deleted.":
    "사용자를 사용 안 함으로 두거나 새 비밀번호를 정하면 30초 안에 로그아웃됩니다. 작업과 기록은 남습니다. 마지막 남은 활성 관리자는 강등·비활성·삭제할 수 없습니다.",

  // ---- system: statistics ----
  "Jobs": "작업", "Completed": "완료", "Failed": "실패", "Cancelled": "취소", "Avg wait": "평균 대기", "Avg run": "평균 실행",
  "No jobs in this period.": "이 기간에 작업이 없습니다.",
  "By group": "그룹별", "By user": "사용자별", "By worker": "워커별", "Busy": "가동률",
  "Jobs count in the period they finished. GPU time = worker held the job (dispatch → finish, model loading included). Busy = share of the period the worker held any job.":
    "작업은 끝난 날짜의 기간에 셉니다. GPU 시간 = 워커가 작업을 잡고 있던 시간(배정 → 종료, 모델 로딩 포함). 가동률 = 기간 중 워커가 작업을 잡고 있던 비율.",

  // ---- system: settings ----
  "Pick the user name from a list on the login page": "로그인 화면에서 사용자 이름을 목록에서 고르기",
  "Login page offers the user names as a drop-down (anyone who can open the login page sees them; passwords are still required)":
    "로그인 화면에 사용자 이름을 드롭다운으로 보여 줍니다 (로그인 화면을 열 수 있는 누구나 이름을 봅니다. 비밀번호는 여전히 필요합니다)",

  // ---- system: rollout ----
  "succeeded": "성공", "aborting": "중단 중", "aborted": "중단됨", "interrupted": "멈춤(확인 필요)", "draining": "비우는 중",
  "restarting": "재시작 중", "starting": "시작 중", "verifying": "검증 중", "done": "완료", "reverting": "되돌리는 중",
  "reverted": "되돌림", "revert failed": "되돌리기 실패", "skipped": "건너뜀", "match": "일치", "differs": "다름",
  "unassigned": "미할당",
  "Start rollout": "롤아웃 시작", "Start a rollout": "롤아웃 시작하기", "Start": "시작",
  "needs a profile and at least one configured instance": "프로필 하나와 설정된 인스턴스가 하나 이상 필요합니다",
  "Roll out profile": "배포할 프로필",
  "Workers (one at a time unless the window is larger)": "워커 (창 크기를 늘리지 않으면 한 대씩)",
  "{worker} — now {profile} ({badge})": "{worker} — 현재 {profile} ({badge})",
  "Window (workers out of service at the same time)": "창 크기 (동시에 서비스에서 빠지는 워커 수)",
  "Give up draining after (minutes, 0 = wait for the running job however long)": "비우기 포기까지 (분, 0 = 실행 중인 작업을 끝까지 기다림)",
  "Wait for the new version at most (minutes)": "새 버전을 기다리는 최대 시간 (분)",
  "Verify": "검증", "also run a tiny smoke workflow on each worker": "워커마다 작은 스모크 워크플로도 실행",
  "Rollout started": "롤아웃을 시작했습니다", "Started, with warnings:": "시작했지만 경고가 있습니다:",
  "Instances": "인스턴스", "Profiles": "프로필", "New profile": "새 프로필", "Audit log": "감사 기록",
  "How the launcher on each host starts the worker. Badge: match = the worker runs exactly its assigned render; differs = it runs something else (not restarted yet, or a failed start).":
    "각 호스트의 런처가 워커를 띄우는 방법입니다. 배지: 일치 = 워커가 할당된 렌더를 그대로 실행 중, 다름 = 다른 것을 실행 중 (아직 재시작 전이거나 시작 실패).",
  "A profile in use cannot be changed — copy it and roll the copy out.": "사용 중인 프로필은 바꿀 수 없습니다 — 복사해서 그 사본을 배포하세요.",
  "Instance": "인스턴스", "Profile": "프로필", "Badge": "배지", "Rev": "리비전", "Restart via": "재시작 방식",
  "Core": "코어", "Venv": "Venv", "Nodes": "노드", "Flags": "플래그", "Assigned": "할당", "When": "시각", "Who": "누가",
  "What": "무엇을", "Target": "대상", "Detail": "내용",
  "Instance of {worker}": "{worker}의 인스턴스",
  "Instance name (the launcher's COMFY_LAUNCH_INSTANCE)": "인스턴스 이름 (런처의 COMFY_LAUNCH_INSTANCE)",
  "Hosts allowed to fetch the render (addresses, comma-separated)": "렌더를 받아 갈 수 있는 호스트 (주소, 쉼표로 구분)",
  "Platform": "플랫폼", "GPU worker": "GPU 워커", "UI worker": "UI 워커",
  "Artifact store <ART>": "아티팩트 저장소 <ART>", "Local root <LOCAL>": "로컬 루트 <LOCAL>", "Models directory": "모델 디렉터리",
  "Input directory": "입력 디렉터리", "Output directory": "출력 디렉터리", "User directory": "사용자 디렉터리", "Temp directory": "임시 디렉터리",
  "{name} (advanced)": "{name} (고급)",
  "Restart configuration (JSON)": "재시작 설정 (JSON)",
  "an operator restarts the worker; the farm waits and watches": "운영자가 워커를 재시작하고, 팜은 기다리며 지켜봅니다",
  "the key file lies in the router's COMFYFARM_KEYS_DIR and is limited to the agent's forced command":
    "키 파일은 라우터의 COMFYFARM_KEYS_DIR 안에 두고, 에이전트의 강제 명령으로만 쓰이게 제한합니다",
  "or \"token_env\": \"COMFYFARM_AGENT_TOKEN_…\"; the token value itself is never stored":
    "또는 \"token_env\": \"COMFYFARM_AGENT_TOKEN_…\". 토큰 값 자체는 저장하지 않습니다",
  "router and worker on the same host": "라우터와 워커가 같은 호스트에 있을 때",
  "restart configuration is not JSON": "재시작 설정이 JSON이 아닙니다", "Instance saved": "인스턴스를 저장했습니다",
  "Assign": "할당", "Assign a profile to {worker}": "{worker}에 프로필 할당",
  "Without a restart: the badge shows 'differs' until the worker restarts. To change a running worker, start a rollout.":
    "재시작하지 않습니다: 워커가 재시작할 때까지 배지는 '다름'으로 보입니다. 실행 중인 워커를 바꾸려면 롤아웃을 시작하세요.",
  "Render of {instance} (rev {rev})": "{instance}의 렌더 (리비전 {rev})",
  "Configure": "설정", "Render": "렌더", "not configured": "설정 안 됨",
  "published: {names}": "게시됨: {names}", "none": "없음",
  "Copy of {name}": "{name} 복사본",
  "Core artifact (ComfyUI release)": "코어 아티팩트 (ComfyUI 릴리스)", "Venv artifact": "Venv 아티팩트",
  "Node artifacts (comma-separated, load order)": "노드 아티팩트 (쉼표로 구분, 로딩 순서)",
  "Frontend artifact (optional)": "프론트엔드 아티팩트 (선택)", "Extra ComfyUI flags": "추가 ComfyUI 플래그",
  "e.g. --preview-method auto --reserve-vram 1 (directories and ports come from the instance)":
    "예: --preview-method auto --reserve-vram 1 (디렉터리와 포트는 인스턴스에서 옵니다)",
  "Environment (KEY=VALUE per line)": "환경 변수 (한 줄에 KEY=VALUE)",
  "only PYTORCH_*, HIP_*, CUDA_*, … keys the launcher allows; values must not be paths":
    "런처가 허용하는 PYTORCH_*, HIP_*, CUDA_* … 키만, 값에는 경로를 쓸 수 없습니다",
  "Profile saved": "프로필을 저장했습니다", "Profile created": "프로필을 만들었습니다", "Copy": "복사",
  "Delete profile {name}?": "{name} 프로필을 삭제할까요?", "in use: only the note can change": "사용 중: 메모만 바꿀 수 있습니다",
  "From → to": "이전 → 이후", "Step": "단계", "Started": "시작", "Took": "소요",
  "Your turn: ": "직접 할 차례: ",
  "restart {instance} on its host now (e.g. systemctl restart comfyfarm@{instance}). The farm watches it go down and come back.":
    "지금 호스트에서 {instance}을(를) 재시작하세요 (예: systemctl restart comfyfarm@{instance}). 팜이 내려갔다 올라오는 것을 지켜봅니다.",
  "No rollout running.": "진행 중인 롤아웃이 없습니다.",
  "Abort": "중단", "Abort? Workers still draining go back to work; restarts already under way finish.":
    "중단할까요? 비우는 중인 워커는 작업으로 돌아가고, 이미 시작된 재시작은 끝까지 진행됩니다.",
  "Aborting": "중단하는 중", "Aborted": "중단했습니다", "Resumed": "재개했습니다",
  "by {who}, {when} · window {window}": "{who}, {when} · 창 {window}",
  "Interrupted: ": "멈춤: ",
  "the router restarted during this rollout. Its workers stay in maintenance until you resume or abort.":
    "롤아웃 도중 라우터가 재시작되었습니다. 재개하거나 중단할 때까지 해당 워커는 점검 상태로 남습니다.",
  "Recent rollouts": "최근 롤아웃",
  "#{id} → {profile} · {done}/{steps} done · {when} · {who}": "#{id} → {profile} · {done}/{steps} 완료 · {when} · {who}",
};
