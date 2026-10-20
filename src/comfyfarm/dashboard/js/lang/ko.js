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

  // ---- worker warnings (sent by the router in English) ----
  "worker extension not installed: open to everyone on the network, no reconnect fix":
    "워커 확장이 설치되지 않음: 네트워크의 누구에게나 열려 있고, 재연결 보정도 없습니다",
  "reconnect fix not active on this ComfyUI version: events may stop after a reconnect":
    "이 ComfyUI 버전에서는 재연결 보정이 동작하지 않음: 재연결 뒤 이벤트가 끊길 수 있습니다",
  "unsecured: accepts requests from any address — set COMFYFARM_ALLOW to the router and gateway":
    "보호되지 않음: 어느 주소의 요청이든 받습니다 — COMFYFARM_ALLOW에 라우터와 게이트웨이 주소를 넣으세요",
  "differs from its group: ": "그룹과 다름: ",
  "custom nodes failed to import (missing on this worker, see its log): ":
    "커스텀 노드를 불러오지 못함 (이 워커에는 없음, 워커 로그 참고): ",

  // ---- worker setup (docs/design/14) ----
  "Set up a worker": "워커 설치", "Set up {name}": "{name} 설치",
  "Turn an empty Linux host into a worker: setup script, host check, then the worker is added":
    "빈 리눅스 호스트를 워커로 만듭니다: 설치 스크립트 → 호스트 점검 → 워커 등록",
  "Worker name (shown in the dashboard)": "워커 이름 (대시보드 표시용)", "Host address": "호스트 주소",
  "the address the router and the gateway reach this host at": "라우터와 게이트웨이가 이 호스트에 닿는 주소",
  "Instance name": "인스턴스 이름", "one ComfyUI on this host; letters, digits, . _ -": "이 호스트의 ComfyUI 하나. 영문·숫자·. _ -",
  "AMD (ROCm)": "AMD (ROCm)", "NVIDIA (CUDA)": "NVIDIA (CUDA)", "Any / not sure": "상관없음 / 모름", "None (CPU only)": "없음 (CPU만)",
  "ComfyFarm checks the driver; it never installs one": "ComfyFarm은 드라이버를 확인만 하고 설치하지 않습니다",
  "Profile to run": "실행할 프로필", "(assign later)": "(나중에 할당)", "ComfyUI port": "ComfyUI 포트", "ssh port": "ssh 포트",
  "From a recipe": "레시피로",
  "Build a recipe on {name}": "{name}에서 레시피 빌드", "Recipe": "레시피", "Build": "빌드",
  "the host downloads ComfyUI, PyTorch and the nodes and publishes them to its artifact store — minutes, mostly the PyTorch download":
    "호스트가 ComfyUI·PyTorch·노드를 받아 자기 아티팩트 저장소에 게시합니다 — 몇 분, 대부분 PyTorch 다운로드",
  "Building {recipe} on {name}": "{name}에서 {recipe} 빌드 중", "template {name}": "견본 {name}",
  "Built — now pick the template": "빌드 완료 — 이제 견본을 고르세요",
  "this worker's tools are older than recipes — Set up again (System → Workers)":
    "이 워커의 도구가 레시피보다 오래됐습니다 — 다시 설치하세요 (시스템 → 워커)",
  "Set up again": "다시 설치", "Set up {name} again": "{name} 다시 설치",
  "kept: setting a worker up again changes its host settings, not its identity": "유지됨: 다시 설치는 호스트 설정만 바꾸고 워커 정체성은 그대로입니다",
  "Worker updated and the profile assigned — restart it (Rollout → Render) or roll it out to use the new tools":
    "워커를 갱신하고 프로필을 배정했습니다 — 새 도구를 쓰려면 재시작(롤아웃 → 렌더)하거나 롤아웃하세요",
  "Profiles from a recipe (read through {name})": "레시피로 프로필 만들기 ({name}을(를) 통해 읽음)",
  "(artifacts missing)": "(아티팩트 없음)", "queued": "대기열에 있음",
  "the artifact store on {name} is not usable: {detail}": "{name}의 아티팩트 저장소를 쓸 수 없습니다: {detail}",
  "Build the recipe on {name} first (no profile template in its store yet)": "먼저 {name}에서 레시피를 빌드하세요 (저장소에 아직 프로필 견본이 없음)",
  "no template is usable: {names}": "쓸 수 있는 견본이 없습니다: {names}",
  "profile {name} exists — kept": "프로필 {name}이(가) 이미 있어 그대로 둡니다",
  "Build {recipe} on {name} first (a new template after a recipe update)": "먼저 {name}에서 {recipe}를 빌드 (레시피 갱신 뒤 새 견본)",
  "Allow builds from the dashboard on this host": "이 호스트에서 대시보드 빌드 허용", "Builds": "빌드",
  "Update the worker": "워커 갱신",
  "the dashboard may then ask this host to build a recipe that setup installs here (it names one, never sends one); a build account owns the artifact store, the service account still only reads it. Needs internet and git on the host; setup brings a pinned uv":
    "대시보드가 이 호스트에 설치된 레시피의 빌드를 요청할 수 있습니다(이름만 고르고 내용은 보내지 않음). 빌드 계정이 아티팩트 저장소를 소유하고, 서비스 계정은 계속 읽기만 합니다. 호스트에 인터넷과 git이 필요하고, uv는 설치가 고정된 버전을 가져옵니다",
  "Correct the setup of {name}": "{name} 설치 값 고치기", "Correct the values": "값 고쳐서 다시",
  // ---- setup form tooltips ----
  "How this worker appears in System → Workers and in the job history.": "시스템 → 워커와 작업 기록에 보이는 이 워커의 이름입니다.",
  "The address (IP or host name) the router and the gateway reach this host at: the router logs in here over ssh, the gateway opens ComfyUI here.":
    "라우터와 게이트웨이가 이 호스트에 닿는 주소(IP나 호스트명)입니다. 라우터는 여기로 ssh 접속하고, 게이트웨이는 여기서 ComfyUI를 엽니다.",
  "Names this ComfyUI on the host — a host can run several, each with its own port and GPU. Used in the unit comfyfarm@<instance> and wherever a path says {instance}.":
    "이 호스트에서 이 ComfyUI의 이름입니다. 한 호스트에 여러 개를 각자의 포트와 GPU로 돌릴 수 있습니다. 유닛 comfyfarm@<인스턴스>와 경로의 {instance}에 쓰입니다.",
  "The kind of GPU this host should have. The host check fails when none of that kind is usable. ComfyFarm checks the driver; it never installs one.":
    "이 호스트에 있어야 할 GPU 종류입니다. 그 종류가 하나도 쓸 수 없으면 호스트 점검이 실패합니다. ComfyFarm은 드라이버를 점검만 하고 설치하지 않습니다.",
  "Jobs run on the workers of a group, and users are allowed groups. Workers of one group must have the same custom nodes and models.":
    "작업은 그룹의 워커에서 돌고, 사용자는 그룹 단위로 권한을 받습니다. 한 그룹의 워커들은 커스텀 노드와 모델이 같아야 합니다.",
  "What this worker runs: a ComfyUI release, a venv and node sets. Leave it for later while nothing is published — System → Rollout → Profiles → From a recipe makes profiles.":
    "이 워커가 돌릴 것: ComfyUI 릴리스, venv, 노드 세트. 아직 게시된 게 없으면 나중에 정하세요 — 시스템 → 롤아웃 → 프로필 → 레시피로에서 프로필을 만듭니다.",
  "The port this ComfyUI listens on — one per instance on a host, and not the router's port when they share a host.":
    "이 ComfyUI가 여는 포트입니다. 호스트의 인스턴스마다 하나씩이고, 라우터와 같은 호스트라면 라우터 포트와 달라야 합니다.",
  "The host's ssh server: the router's restart key logs in here.": "호스트의 ssh 서버 포트입니다. 라우터의 재시작 키가 여기로 접속합니다.",
  "A dedicated account that runs ComfyUI; setup creates it (or uses one that exists). Never a person's login account: its authorized_keys gets the router's key.":
    "ComfyUI를 돌리는 전용 계정입니다. 설치가 만들거나 이미 있는 계정을 씁니다. 사람의 로그인 계정은 안 됩니다 — 그 계정의 authorized_keys에 라우터 키가 들어갑니다.",
  "The same number on every host, so that files on shared storage belong to one owner everywhere.":
    "모든 호스트에서 같은 번호여야 공유 스토리지의 파일이 어디서나 같은 소유자가 됩니다.",
  "The router as this host reaches it: http://<control-plane address>:<router port>. The launcher fetches what to run from here.":
    "이 호스트에서 본 라우터 주소: http://<제어 서버 주소>:<라우터 포트>. 런처가 여기서 실행할 내용을 받아 갑니다.",
  "Where published ComfyUI releases, venvs and node sets live — at the same path on every host (a shared mount or a copy).":
    "게시된 ComfyUI 릴리스·venv·노드 세트가 있는 곳입니다. 모든 호스트에서 같은 경로여야 합니다(공유 마운트나 복사본).",
  "This host's own disk: its copies of ComfyUI and the node sets, and run files.": "이 호스트 자신의 디스크: ComfyUI와 노드 세트의 로컬 복사본, 실행 파일.",
  "The models ComfyUI reads (checkpoints, LoRAs, VAE …). Read only.": "ComfyUI가 읽는 모델(체크포인트, LoRA, VAE …). 읽기만 합니다.",
  "Uploads. Shared by every worker and the UI worker: the same storage everywhere — never {instance}. Mount it before setup: setup never mounts storage.":
    "업로드. 모든 워커와 UI 워커가 함께 쓰는 같은 스토리지여야 합니다 — {instance}를 넣지 마세요. 설치 전에 직접 마운트하세요. 설치는 스토리지를 마운트하지 않습니다.",
  "Results. Shared by every worker and the UI worker: the same storage everywhere — never {instance}. Mount it before setup: setup never mounts storage.":
    "결과물. 모든 워커와 UI 워커가 함께 쓰는 같은 스토리지여야 합니다 — {instance}를 넣지 마세요. 설치 전에 직접 마운트하세요. 설치는 스토리지를 마운트하지 않습니다.",
  "Users' workflows and settings. Shared by every worker and the UI worker — never {instance}. Mount it before setup: setup never mounts storage.":
    "사용자의 워크플로와 설정. 모든 워커와 UI 워커가 함께 씁니다 — {instance}를 넣지 마세요. 설치 전에 직접 마운트하세요. 설치는 스토리지를 마운트하지 않습니다.",
  "Previews and other temporary files of this instance, on this host. {instance} becomes the instance name.":
    "이 인스턴스의 미리보기 등 임시 파일(이 호스트). {instance}는 인스턴스 이름으로 바뀝니다.",
  "Lets System → Rollout → Profiles → From a recipe ask this host to build a recipe installed here. A separate build account owns the artifact store; ComfyUI's account only reads it. Needs internet and git; setup brings a pinned uv.":
    "시스템 → 롤아웃 → 프로필 → 레시피로에서 이 호스트에 설치된 레시피를 빌드하게 합니다. 별도 빌드 계정이 아티팩트 저장소를 소유하고, ComfyUI 계정은 읽기만 합니다. 인터넷과 git이 필요하고, uv는 설치가 고정 버전을 가져옵니다.",
  "Worker updated — the new worker tools are used from its next start (Rollout → Render, or a rollout)":
    "워커를 갱신했습니다 — 새 워커 도구는 다음 시작부터 쓰입니다 (롤아웃 → 렌더, 또는 롤아웃)",
  "this worker was not set up from the dashboard — Set up a worker, with another name":
    "이 워커는 대시보드로 설치되지 않았습니다 — 다른 이름으로 워커 설치를 하세요", "Profiles from a recipe": "레시피로 프로필 만들기", "Template": "견본",
  "Create both profiles": "프로필 두 개 만들기", "GPU workers": "GPU 워커",
  "no worker to read the artifact store through yet — set one up first (System → Workers)":
    "아티팩트 저장소를 읽을 워커가 아직 없습니다 — 먼저 워커를 설치하세요 (시스템 → 워커)",
  "the addresses the workers take requests from: the router's and the gateway's, as the workers see them":
    "워커가 요청을 받는 주소: 워커에서 본 라우터와 게이트웨이 주소",
  "Profiles created: {names} — pick them in Set up a worker, or Assign": "프로필을 만들었습니다: {names} — 워커 설치나 배정에서 고르세요",
  "Service account (its own, made by setup)": "서비스 계정 (전용, 설치가 만듦)",
  "not your login account: setup creates it to run ComfyUI, and only the router's key — restart, query, check — logs in as it":
    "본인 로그인 계정이 아닙니다: 설치가 ComfyUI 실행용으로 만들고, 라우터 키(재시작·조회·점검)만 이 계정으로 접속합니다",
  "your own admin account here, not the service account": "여기는 서비스 계정이 아니라 본인의 관리자 계정입니다",
  "Router address as the worker sees it": "워커가 보는 라우터 주소",
  "Artifact store <ART> (same path on every host)": "아티팩트 저장소 <ART> (모든 호스트에서 같은 경로)",
  "Local root <LOCAL> (this host's disk)": "로컬 루트 <LOCAL> (이 호스트의 디스크)",
  "Input directory (shared)": "입력 디렉터리 (공유)", "Output directory (shared)": "출력 디렉터리 (공유)",
  "User directory (shared)": "사용자 디렉터리 (공유)", "Temp directory (this host)": "임시 디렉터리 (이 호스트)",
  "Next": "다음",
  "Run a command yourself (recommended)": "직접 명령 실행 (권장)",
  "You paste one line on the host. Nothing on the router ever gets root on your workers.":
    "호스트에 한 줄을 붙여 넣습니다. 라우터는 워커의 root 권한을 한 번도 갖지 않습니다.",
  "Let the router log in once": "라우터가 한 번 로그인",
  "Quicker for many hosts. You type an admin account and password; for those minutes the router has root on the host.":
    "호스트가 많을 때 빠릅니다. 관리자 계정과 비밀번호를 입력하면, 설치하는 몇 분 동안 라우터가 그 호스트의 root 권한을 갖습니다.",
  "On the new host, as a user who may use sudo, run:": "새 호스트에서 sudo를 쓸 수 있는 사용자로 실행하세요:",
  "Copied": "복사했습니다",
  "It downloads the setup script from the router, checks it against this checksum and runs it with sudo. The router never holds root on the host. Valid for 2 hours.":
    "라우터에서 설치 스크립트를 받아 이 체크섬과 맞는지 확인한 뒤 sudo로 실행합니다. 라우터는 호스트의 root 권한을 갖지 않습니다. 2시간 동안 유효합니다.",
  "Your admin account on the host (may use sudo)": "호스트의 본인 관리자 계정 (sudo 가능)", "Log in and run": "로그인해서 실행",
  "account and password, please": "계정과 비밀번호를 입력하세요",
  "The router logs in with this account and runs the same script with sudo. The password is used for this run only: it is not stored, logged or shown again. While setup runs, the router has root on this host.":
    "라우터가 이 계정으로 로그인해 같은 스크립트를 sudo로 실행합니다. 비밀번호는 이번 실행에만 쓰고 저장·기록·재표시하지 않습니다. 설치가 도는 동안 라우터가 이 호스트의 root 권한을 갖습니다.",
  "reported": "보고됨", "finished": "완료", "replaced": "교체됨",
  "waiting for the host to run the script…": "호스트에서 스크립트가 실행되기를 기다리는 중…",
  "the router is running the script on the host…": "라우터가 호스트에서 스크립트를 실행하는 중…",
  "the host is ready": "호스트 준비 완료",
  "setup or the host check failed — fix the host and run the same command again": "설치 또는 호스트 점검 실패 — 호스트를 고치고 같은 명령을 다시 실행하세요",
  "the worker was added": "워커를 등록했습니다", "this setup expired — make a new one": "이 설치는 만료되었습니다 — 새로 만드세요",
  "Error: ": "오류: ", "Add the worker": "워커 등록",
  "Worker added and starting — watch its badge in System → Rollout": "워커를 등록하고 시작하는 중 — 시스템 → 롤아웃에서 배지를 확인하세요",
  "Worker added — assign a profile and start it in System → Rollout": "워커를 등록했습니다 — 시스템 → 롤아웃에서 프로필을 할당하고 시작하세요",
  "The worker is not running: it starts with this profile.": "워커가 실행 중이 아닙니다. 이 프로필로 시작합니다.",
  "Start it now": "지금 시작", "Assign and start": "할당하고 시작",
  "Starting {worker} — its badge turns to match when it is up": "{worker} 시작 중 — 올라오면 배지가 일치로 바뀝니다",
  "The worker is not running: start it with its assigned profile": "워커가 실행 중이 아닙니다. 할당된 프로필로 시작합니다",
  "Check host": "호스트 점검", "Host check: {name}": "호스트 점검: {name}",
  "Read-only check of the host through its agent: driver, groups, directories, venvs, clock, router":
    "에이전트를 통한 읽기 전용 점검: 드라이버, 그룹, 디렉터리, venv, 시계, 라우터",
  "OK": "정상", "WARN": "주의", "FAIL": "실패",
  "Service account uid": "서비스 계정 UID",
  "the same number on every host: shared storage checks numbers, not names": "모든 호스트에서 같은 번호로: 공유 스토리지는 이름이 아니라 번호로 권한을 봅니다",
  "What will this host be?": "이 호스트의 역할은?",
  "GPU worker — runs the jobs": "GPU 워커 — 작업을 실행합니다",
  "UI worker — serves the screen and the users' files (no GPU, no jobs)": "UI 워커 — 화면과 사용자 파일을 맡습니다 (GPU·작업 없음)",
  "A farm has one UI worker (often on the control-plane host) and any number of GPU workers. The gateway finds the UI worker by itself.":
    "팜에는 UI 워커 하나(보통 제어 호스트)와 GPU 워커 여러 대가 있습니다. 게이트웨이는 UI 워커를 스스로 찾습니다.",
  "Set up the UI worker": "UI 워커 설치", "Set up a GPU worker": "GPU 워커 설치",
  "UI profile: the core of the GPU workers, a CPU venv, nodes: comfyfarm_ui and comfyfarm_worker, flag --cpu, env COMFYFARM_ALLOW=<gateway and router addresses>":
    "UI용 프로필: GPU 워커와 같은 코어, CPU venv, 노드 comfyfarm_ui·comfyfarm_worker, 플래그 --cpu, env COMFYFARM_ALLOW=<게이트웨이·라우터 주소>",
  "not 8188 when it shares the host with the router": "라우터와 같은 호스트라면 8188은 피하세요",
  "Continue: {name} on {host} ({status})": "이어서: {host}의 {name} ({status})",
  "(the host has reported — no need to run the script again)": "(호스트가 이미 보고했습니다 — 스크립트를 다시 실행할 필요 없음)",
  "http://<router address>:<router port> — the address the launcher fetches its render from": "http://<라우터 주소>:<라우터 포트> — 런처가 렌더를 받아 가는 주소",
};
