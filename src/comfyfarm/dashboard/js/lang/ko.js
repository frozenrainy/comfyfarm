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
  "unknown": "알 수 없음", "busy unresponsive": "바쁨 (응답 없음)", "suspect": "연결 의심",

  // ---- common ----
  "Save": "저장", "Saved": "저장했습니다", "Cancel": "취소", "Close": "닫기", "Create": "만들기", "Add": "추가",
  "Edit": "편집", "Delete": "삭제", "Deleted": "삭제했습니다", "Refresh": "새로 고침", "Enable": "사용", "Disable": "사용 안 함",
  "Edit {name}": "{name} 편집",
  "none available": "선택할 수 있는 항목이 없습니다",
  "note": "메모", "Note": "메모", "Note saved": "메모를 저장했습니다",

  // ---- job tables ----
  "Status": "상태", "Owner": "소유자", "Group": "그룹", "Worker": "워커", "Progress": "진행",
  "Waiting / running": "대기 / 실행 시간", "Finished": "종료", "Run time": "실행 시간", "Error": "오류", "Result": "결과", "{n} file(s)": "파일 {n}개",
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
  "{n} job(s)": "{n}건",
  "No worker group yet: set up the first GPU worker (System → Workers) — it makes the first group.":
    "아직 워커 그룹이 없습니다: 첫 GPU 워커를 설치하세요 (시스템 → 워커) — 그때 첫 그룹이 생깁니다.", "{n} worker(s) differ": "워커 {n}대가 다름",

  // ---- dashboard ----
  "My jobs": "내 작업", "My groups": "내 그룹", "My usage": "내 사용량", "Recent": "최근", "All history →": "전체 기록 →",
  "Nothing queued. Submit from ComfyUI.": "대기 중인 작업이 없습니다. ComfyUI에서 실행하세요.",
  "No finished jobs yet.": "아직 끝난 작업이 없습니다.",
  "jobs finished": "끝난 작업", "GPU time": "GPU 시간", "avg wait": "평균 대기", "avg run": "평균 실행",
  "{date}: {completed} done, {failed} failed, {cancelled} cancelled": "{date}: 완료 {completed}, 실패 {failed}, 취소 {cancelled}",

  // ---- queue / history ----
  "all unfinished": "끝나지 않은 전체", "all": "전체", "all groups": "모든 그룹", "all users": "모든 사용자",
  "No matching jobs.": "조건에 맞는 작업이 없습니다.",
  "search title, id or note": "제목, ID, 메모 검색", "search title, id, note, owner": "제목, ID, 메모, 소유자 검색",
  "search title, id, note or error": "제목, ID, 메모, 오류 검색", "search title, id, note, error, owner": "제목, ID, 메모, 오류, 소유자 검색",
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
  "out of date: read again when a worker of the group answers (or Refresh nodes)": "오래됨: 그룹 워커가 응답하면 다시 읽습니다 (또는 노드 목록 새로 고침)",
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
  "Core": "코어", "Venv": "가상환경(venv)", "Nodes": "노드", "Flags": "플래그", "Assigned": "할당", "When": "시각", "Who": "누가",
  "What": "무엇을", "Target": "대상", "Detail": "내용",
  "Instance of {worker}": "{worker}의 인스턴스",
  "Instance name (the launcher's COMFY_LAUNCH_INSTANCE)": "인스턴스 이름 (런처의 COMFY_LAUNCH_INSTANCE)",
  "Hosts allowed to fetch the render (addresses, comma-separated)": "렌더를 받아 갈 수 있는 호스트 (주소, 쉼표로 구분)",
  "Platform": "플랫폼", "GPU worker": "GPU 워커", "UI worker": "UI 워커",
  "pick at least one worker": "워커를 하나 이상 고르세요",
  "Artifact store <ART>": "아티팩트 저장소 <ART>", "Local root <LOCAL>": "로컬 루트 <LOCAL>", "Models directory": "모델 디렉터리", "Model store (models from the dashboard)": "모델 저장소 (대시보드에서 추가한 모델)",
  "empty: no models from the dashboard on this host": "비워 두면: 이 호스트에는 대시보드 모델을 두지 않음",
  "The models added in System → Models. Copied: the ones this worker's group uses are copied here, checked against the builder's signature (a directory of its own, not the models directory above). Mounted: mount the control plane's model store here. Empty: none on this host.":
    "시스템 → 모델에서 추가한 모델. 복사: 이 워커 그룹이 쓰는 모델이 여기로 복사되고 빌더 서명으로 검증됩니다 (위 모델 디렉터리와 다른 별도 디렉터리). 마운트: 제어 영역의 모델 저장소를 여기에 마운트하세요. 비워 두면: 이 호스트에는 두지 않음.",
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
  "event stream down: no progress or previews": "이벤트 스트림 끊김: 진행률과 미리보기가 오지 않습니다",
  "no group: receives no jobs": "그룹 없음: 작업을 받지 않습니다",
  "does not meet its group's requirements: receives no jobs": "그룹의 요구 사항을 채우지 못함: 작업을 받지 않습니다",
  "refuses the router: its allow-list does not have {addr} — add that address to COMFYFARM_ALLOW_NETWORK in the host's /etc/comfyfarm/<instance>.env (Set up again writes it) or to the profile's COMFYFARM_ALLOW, then restart the worker":
    "라우터를 거부함: 허용 목록에 {addr}이(가) 없습니다 — 호스트의 /etc/comfyfarm/<instance>.env의 COMFYFARM_ALLOW_NETWORK(다시 설치하면 다시 씀)나 프로필의 COMFYFARM_ALLOW에 그 주소를 넣고 워커를 재시작하세요",
  "refused {n} request(s), the last from {addr} — if that is this farm's gateway or router, add it to COMFYFARM_ALLOW_NETWORK in the host's /etc/comfyfarm/<instance>.env or to the profile's COMFYFARM_ALLOW":
    "요청 {n}건을 거부함, 마지막은 {addr} — 이 팜의 게이트웨이나 라우터라면 호스트의 /etc/comfyfarm/<instance>.env의 COMFYFARM_ALLOW_NETWORK나 프로필의 COMFYFARM_ALLOW에 넣으세요",
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
  "Profiles from a recipe (read through {name})": "레시피로 프로필 만들기 ({name}을(를) 통해 읽음)",
  "(artifacts missing)": "(아티팩트 없음)", "queued": "대기열에 있음",
  "the artifact store on {name} is not usable: {detail}": "{name}의 아티팩트 저장소를 쓸 수 없습니다: {detail}",
  "Build the recipe on {name} first (no profile template in its store yet)": "먼저 {name}에서 레시피를 빌드하세요 (저장소에 아직 프로필 견본이 없음)",
  "no template is usable: {names}": "쓸 수 있는 견본이 없습니다: {names}",
  "({names} existed already — kept)": "({names}은(는) 이미 있어 그대로 둠)",
  "{names}: these profiles exist already — this template made them. Roll them out as they are; for new node packs or a new recipe, build again (the checkbox below the template).": "{names}: 이 견본으로 만든 프로필이 이미 있습니다. 그대로 롤아웃하세요. 노드팩이나 레시피가 바뀌었다면 다시 빌드하세요(견본 아래 체크박스).",
  "Allow builds from the dashboard on this host": "이 호스트에서 대시보드 빌드 허용", "Builds": "빌드",
  "Update the worker": "워커 갱신",
  "Restart {worker}? It finishes its running job first (a rollout of its own profile).": "{worker}를 재시작할까요? 실행 중인 작업을 먼저 끝냅니다 (자기 프로필 롤아웃).",
  "Restarting {worker} — see Rollout": "{worker} 재시작 중 — 롤아웃에서 확인하세요",
  "Restart": "재시작",
  "Drain, restart and verify it on its own profile — after Set up again, to use the new tools": "자기 프로필로 비우고, 재시작하고, 검증합니다 — 다시 설치 후 새 도구를 쓰려면",
  "Worker updated — assign a profile and start it in System → Rollout": "워커를 갱신했습니다 — 시스템 → 롤아웃에서 프로필을 할당하고 시작하세요",
  "Worker updated and starting": "워커를 갱신하고 시작합니다",
  "Worker updated, not restarted yet: {why} — Restart it in System → Rollout": "워커를 갱신했지만 아직 재시작하지 않았습니다: {why} — 시스템 → 롤아웃에서 재시작하세요",
  "Restart {name} now?": "{name}를 지금 재시작할까요?",
  "Start {name} now?": "{name}를 지금 시작할까요?",
  "The new worker tools and settings take effect when it starts again. A rollout of its own profile restarts it: it finishes its running job first, then it is verified.": "새 워커 도구와 설정은 다시 시작할 때 적용됩니다. 자기 프로필 롤아웃으로 재시작합니다: 실행 중인 작업을 먼저 끝내고, 그다음 검증합니다.",
  "The new worker tools and settings take effect when it starts.": "새 워커 도구와 설정은 시작할 때 적용됩니다.",
  "Restart now": "지금 재시작",
  "Start now": "지금 시작",
  "Worker updated — restart it later: System → Rollout → Restart": "워커를 갱신했습니다 — 나중에 재시작하세요: 시스템 → 롤아웃 → 재시작",
  "Restarting it — see Rollout": "재시작 중 — 롤아웃에서 확인하세요",
  "Worker added — make a profile (System → Rollout → From a recipe), then assign and start it": "워커를 등록했습니다 — 프로필을 만든 뒤(시스템 → 롤아웃 → 레시피로) 할당하고 시작하세요",
  "what it runs; a later change goes through a rollout": "이 워커가 실행할 것. 나중에 바꿀 때는 롤아웃으로 합니다",
  "What one build left in the artifact store: ComfyUI, a venv and the node sets for the GPU workers and the UI worker. The newest is last.": "빌드 한 번이 아티팩트 저장소에 남긴 것: GPU 워커와 UI 워커용 ComfyUI, venv, 노드 세트. 가장 새것이 마지막입니다.",
  "Optional: addresses the workers take requests from, added to what setup allowed on each host (the router's address). Leave it empty unless a worker reaches the router or the gateway over another address.": "선택: 워커가 요청을 받을 주소. 설치가 각 호스트에 허용한 주소(라우터 주소)에 더해집니다. 워커가 다른 주소로 라우터나 게이트웨이에 닿는 경우가 아니면 비워 두세요.",
  "Build the chosen template's recipe again on {name} first": "먼저 {name}에서 고른 견본의 레시피를 다시 빌드",
  "Makes a new template instead of using one of these: after adding or updating node packs, or after a ComfyFarm upgrade that changed the recipe. The build takes minutes; its profiles then roll out like any.": "여기 있는 견본 대신 새 견본을 만듭니다: 노드팩을 추가하거나 업데이트한 뒤, 또는 ComfyFarm 업그레이드로 레시피가 바뀐 뒤에 쓰세요. 빌드는 몇 분 걸리고, 만든 프로필은 다른 프로필처럼 롤아웃합니다.",
  "no group yet: this one is made with the worker (jobs go to a group's workers; users get groups)": "아직 그룹이 없습니다: 이 이름으로 워커와 함께 만듭니다 (작업은 그룹의 워커로 가고, 사용자는 그룹 단위로 권한을 받습니다)",
  "The UI worker is ready": "UI 워커 준비",
  "ComfyUI opens in the browser. It builds and starts by itself on the first start — a minute or two.": "브라우저에서 ComfyUI가 열립니다. 첫 기동 때 스스로 빌드하고 시작합니다 — 1~2분.",
  "A build for your GPU, and its profiles": "내 GPU용 빌드와 프로필",
  "System → Rollout: add node packs if you want them, Build for your kind of GPU, then From a recipe.": "시스템 → 롤아웃: 원하면 노드팩을 추가하고, GPU 종류에 맞게 빌드한 뒤 레시피로 프로필을 만듭니다.",
  "A GPU worker running": "GPU 워커 실행",
  "System → Workers → Set up a worker: the GPU machine (this one, too). It makes the first group.": "시스템 → 워커 → 워커 설치: GPU 머신을 등록합니다(이 서버도 됩니다). 첫 그룹도 함께 만듭니다.",
  "An artist account": "아티스트 계정",
  "System → Users: an account with the group; artists sign in at this address.": "시스템 → 사용자: 그룹이 있는 계정. 아티스트는 이 주소로 로그인합니다.",
  "A first job": "첫 작업",
  "Open ComfyUI, pick the group, queue a workflow.": "ComfyUI를 열고 그룹을 고른 뒤 워크플로를 실행합니다.",
  "Open ComfyUI ↗": "ComfyUI 열기 ↗",
  "Go →": "이동 →",
  "Getting started": "시작하기",
  "{n} of {m}": "{m}단계 중 {n}",
  "Hide": "숨기기",
  "Which PyTorch the GPU workers get: rocm-… for AMD, cuda-… for NVIDIA (see the description). The UI worker's part is the same in every recipe.": "GPU 워커가 받을 PyTorch: AMD는 rocm-…, NVIDIA는 cuda-… (설명 참고). UI 워커 몫은 모든 레시피가 같습니다.",
  // ---- node packs, control-plane builds (docs/design/15) ----
  "Add a node pack": "노드팩 추가",
  "Add node pack": "노드팩 추가",
  "Added packs are built into node sets and venvs by Build, then rolled out like any profile — the UI worker first. Allowed sources: {s}": "추가한 팩은 빌드가 노드 세트와 venv로 만들고, 다른 프로필처럼 롤아웃합니다 — UI 워커 먼저. 허용된 저장소: {s}",
  "Already the newest commit of {ref}": "이미 {ref}의 최신 커밋입니다",
  "Also on the UI worker": "UI 워커에도 설치",
  "Branch, tag or commit (empty: the default branch)": "브랜치, 태그 또는 커밋 (비우면 기본 브랜치)",
  "Build on the control plane": "제어 영역에서 빌드",
  "Building {recipe} on the control plane": "제어 영역에서 {recipe} 빌드 중",
  "Commit": "커밋",
  "Constraints (optional, comma-separated)": "버전 제한 (선택, 쉼표로 구분)",
  "For": "대상",
  "GPU workers and the UI worker": "GPU 워커와 UI 워커",
  "GPU workers only": "GPU 워커만",
  "Git repository (https)": "Git 저장소 (https)",
  "Look up and add": "조회해서 추가",
  "Looking up {ref}…": "{ref} 조회 중…",
  "Name (its directory under custom_nodes; empty: from the repository)": "이름 (custom_nodes 아래 폴더 이름. 비우면 저장소 이름)",
  "Node pack": "노드팩",
  "Node pack added — Build to put it into the artifact store": "노드팩을 추가했습니다 — 빌드하면 아티팩트 저장소에 들어갑니다",
  "Node pack {name}": "노드팩 {name}",
  "Node packs": "노드팩",
  "Off for a pack that imports only with a GPU (the UI worker runs on the CPU) — a rollout to the UI worker names such a pack": "GPU가 있어야 로딩되는 팩은 끄세요(UI 워커는 CPU로 돕니다) — UI 워커 롤아웃이 그런 팩을 알려 줍니다",
  "Recipe (the kind of GPU)": "레시피 (GPU 종류)",
  "Remove": "제거",
  "Remove node pack {name}? Profiles built with it keep it until you build again.": "노드팩 {name}을 제거할까요? 이 팩으로 만든 프로필은 다시 빌드할 때까지 그대로입니다.",
  "Removed": "제거했습니다",
  "Repository": "저장소",
  "Requirements file (empty: requirements.txt, else pyproject.toml)": "요구사항 파일 (비우면 requirements.txt, 없으면 pyproject.toml)",
  "Saved — Build to use it": "저장했습니다 — 빌드해야 반영됩니다",
  "The builder is not running — node packs cannot be looked up or built.": "빌더가 실행 중이 아닙니다 — 노드팩을 조회하거나 빌드할 수 없습니다.",
  "This farm's builder takes no node packs from the dashboard (COMFYFARM_BUILD_NODE_SOURCES is empty).": "이 팜의 빌더는 대시보드의 노드팩을 받지 않습니다 (COMFYFARM_BUILD_NODE_SOURCES가 비어 있음).",
  "UI worker: ": "UI 워커: ",
  "Update": "업데이트",
  "Updated — Build to use it": "업데이트했습니다 — 빌드해야 반영됩니다",
  "Without the control-plane builder (the compose deployment's), node packs go into recipes (rollouts.md).": "제어 영역 빌더(compose 배포의 것)가 없으면 노드팩은 레시피에 넣습니다 (rollouts.md).",
  "allowed here: {sources}": "허용된 저장소: {sources}",
  "could not look it up": "조회하지 못했습니다",
  "hold a dependency back where the pack breaks on a newer one": "새 버전에서 팩이 깨지는 의존성을 묶어 둡니다",
  "no control-plane builder here — build on a worker host (From a recipe) or with publish.py": "제어 영역 빌더가 없습니다 — 워커 호스트(레시피로)나 publish.py로 빌드하세요",
  "no template in the artifact store yet": "아티팩트 저장소에 아직 템플릿이 없습니다",
  "no — GPU only": "아니요 — GPU 전용",
  "pinned to the commit it names now; Update moves it to a newer one": "지금 가리키는 커밋으로 고정됩니다. 업데이트로 새 커밋으로 옮깁니다",
  "the UI worker only": "UI 워커만",
  "the builder did not answer in time": "빌더가 제때 답하지 않았습니다",
  "the builder is not running (docker compose ps builder)": "빌더가 실행 중이 아닙니다 (docker compose ps builder)",
  "the builder is not running — node packs are looked up and built by it": "빌더가 실행 중이 아닙니다 — 노드팩 조회와 빌드는 빌더가 합니다",
  "the control plane": "제어 영역",
  "the default branch": "기본 브랜치",
  "this farm's builder takes no node packs from the dashboard (COMFYFARM_BUILD_NODE_SOURCES)": "이 팜의 빌더는 대시보드의 노드팩을 받지 않습니다 (COMFYFARM_BUILD_NODE_SOURCES)",
  "with this farm's node packs ({n})": "이 팜의 노드팩 포함 ({n}개)",
  "GPU only": "GPU 전용",
  "none added yet — Add node pack first (System → Rollout → Node packs)": "아직 추가한 노드팩이 없습니다 — 먼저 노드팩을 추가하세요 (시스템 → 롤아웃 → 노드팩)",
  "yes": "예",
  "building": "빌드 중",
  "error": "오류",
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
  "this worker was not set up from the dashboard — Set up a worker, with another name":
    "이 워커는 대시보드로 설치되지 않았습니다 — 다른 이름으로 워커 설치를 하세요", "Profiles from a recipe": "레시피로 프로필 만들기", "Template": "견본",
  "Create the profiles": "프로필 만들기", "GPU workers": "GPU 워커",
  "no worker to read the artifact store through yet — set one up first (System → Workers)":
    "아티팩트 저장소를 읽을 워커가 아직 없습니다 — 먼저 워커를 설치하세요 (시스템 → 워커)",
  "the addresses the workers take requests from: the router's and the gateway's, as the workers see them":
    "워커가 요청을 받는 주소: 워커에서 본 라우터와 게이트웨이 주소",
  "Profiles created: {names} — roll them out (UI worker first), or pick them in Set up a worker": "프로필을 만들었습니다: {names} — 롤아웃하세요(UI 워커 먼저). 워커 설치에서 고를 수도 있습니다",
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
  "Continue: {name} on {host} ({status}, {at})": "이어서: {host}의 {name} ({status}, {at})",
  "ComfyUI listens on": "ComfyUI 수신 주소",
  "Artifact store on this host": "이 호스트의 아티팩트 저장소",
  "Copied here by the farm (no share needed)": "팜이 여기로 복사 (공유 저장소 필요 없음)",
  "Mounted here — a share (NFS, SMB) at the same path": "여기에 마운트 — 같은 경로의 공유 (NFS, SMB)",
  "copies: what a profile needs is copied before it starts, checked against the builder's signature; the first start of a new build waits for the copy":
    "복사: 프로필에 필요한 것을 시작 전에 복사하고 빌더 서명으로 검증합니다. 새 빌드의 첫 시작은 복사를 기다립니다",
  "this farm has no builder that signs: mount the control plane's store here (or copy it yourself)":
    "이 팜에는 서명하는 빌더가 없습니다: 제어 영역 저장소를 여기에 마운트하세요 (또는 직접 복사)",
  "Copied: this host keeps its own copy of the artifact store at the same path, filled by the farm with what its profiles need — checked file by file against the builder's signature, so nothing else can be slipped in. Mounted: you mount the control plane's store there (NFS, a share); the farm copies nothing.":
    "복사: 이 호스트가 같은 경로에 자체 사본을 두고, 팜이 프로필에 필요한 것을 채웁니다 — 파일마다 빌더 서명으로 검증하므로 다른 것이 끼어들 수 없습니다. 마운트: 제어 영역 저장소를 직접 마운트합니다 (NFS, 공유). 팜은 아무것도 복사하지 않습니다.",
  "fetching": "복사 중",
  "the control plane's: {path}": "제어 영역의 값: {path}",
  "The control plane publishes to {path}: every host must see the artifact store at that same path (mount or copy it there)":
    "제어 영역은 {path}에 게시합니다: 모든 호스트가 아티팩트 저장소를 바로 그 경로로 봐야 합니다 (그 경로에 마운트하거나 복사하세요)",
  "The UI worker uses {path}: every host must use that same storage at that same path, or uploads and results go missing":
    "UI 워커는 {path}를 씁니다: 모든 호스트가 같은 저장소를 같은 경로로 써야 합니다. 아니면 업로드와 결과물이 보이지 않습니다",
  "Some paths differ from the control plane's — correct them, or press Next again to keep them":
    "제어 영역과 다른 경로가 있습니다 — 고치거나, 그대로 두려면 다음을 한 번 더 누르세요",
  "0.0.0.0: every address of the host (the worker's allow-list still refuses all but the router and the gateway); this host's address: that one only":
    "0.0.0.0: 호스트의 모든 주소 (워커의 허용 목록이 라우터와 게이트웨이 말고는 여전히 거부합니다). 이 호스트 주소를 쓰면 그 주소에서만 받습니다",
  "ComfyUI's --listen. 0.0.0.0 answers on every address of the host — the worker's allow-list refuses everyone but the router and the gateway either way. One address (this host's) when your site wants nothing open on all addresses.":
    "ComfyUI의 --listen. 0.0.0.0은 호스트의 모든 주소에서 받습니다 — 어느 쪽이든 워커의 허용 목록이 라우터와 게이트웨이 말고는 거부합니다. 사이트 규칙상 모든 주소에 열어 두면 안 된다면 이 호스트의 주소 하나를 넣으세요.",
  "not started yet": "아직 시작 전", "manual": "수동", "Show": "보기", "Show the log": "로그 보기",
  "— pick the kind of GPU your workers have —": "— 워커의 GPU 종류를 고르세요 —",
  "optional — usually empty: setup already lets the router and the gateway in on each host":
    "선택 — 보통 비워 둡니다: 설치가 이미 각 호스트에서 라우터와 게이트웨이를 허용합니다",
  "Build of {recipe} on the control plane — {min} min so far": "제어 영역에서 {recipe} 빌드 중 — {min}분 경과",
  "Build of {recipe} done: template {template}": "{recipe} 빌드 완료: 견본 {template}",
  "Build of {recipe} failed: {error}": "{recipe} 빌드 실패: {error}",
  "A build of {recipe} is running: its template appears here when it is done": "{recipe} 빌드가 진행 중입니다: 끝나면 그 견본이 여기에 나타납니다",
  "Profiles created: {names} — next, Set up a worker picks the GPU profile (the UI worker already runs what it needs)":
    "프로필을 만들었습니다: {names} — 다음은 워커 설치에서 GPU 프로필을 고르세요 (UI 워커는 이미 필요한 것을 실행 중)",
  "Profiles created: {names} — roll the UI profile out to the UI worker (Start rollout) for the node packs; the GPU profile goes to GPU workers (Set up a worker, or a rollout)":
    "프로필을 만들었습니다: {names} — 노드팩을 쓰려면 UI 프로필을 UI 워커에 롤아웃하세요(롤아웃 시작). GPU 프로필은 GPU 워커용입니다(워커 설치 또는 롤아웃)",
  // ---- host check (hostcheck.py sends each line's English template and values) ----
  "GPU (AMD)":
    "GPU (AMD)",
  "GPU (NVIDIA)":
    "GPU (NVIDIA)",
  "artifact store":
    "아티팩트 저장소",
  "builds":
    "빌드",
  "clock":
    "시계",
  "groups":
    "그룹",
  "lingering":
    "상주(linger)",
  "local root":
    "로컬 루트",
  "models":
    "모델",
  "port":
    "포트",
  "python":
    "python",
  "recipes":
    "레시피",
  "router":
    "라우터",
  "user manager":
    "사용자 관리자",
  "input directory":
    "입력 디렉터리",
  "output directory":
    "출력 디렉터리",
  "user directory":
    "사용자 디렉터리",
  "temp directory":
    "임시 디렉터리",
  "{version} for the launcher":
    "런처용 {version}",
  "{version} for the launcher — needs {need} or newer":
    "런처용 {version} — {need} 이상이 필요합니다",
  "{user}: on — the instance starts at boot":
    "{user}: 켜짐 — 부팅하면 인스턴스가 시작됩니다",
  "{user}: off — the user manager stops at logout (setup runs `loginctl enable-linger`)":
    "{user}: 꺼짐 — 로그아웃하면 사용자 관리자가 멈춥니다 (설치가 `loginctl enable-linger`를 실행합니다)",
  "systemd --user: {state}":
    "systemd --user: {state}",
  "systemd --user: not reachable":
    "systemd --user: 연결할 수 없음",
  "setup expects an {kind} GPU and none is usable here (driver installed and loaded?)":
    "설치는 {kind} GPU를 기대하는데 여기서 쓸 수 있는 것이 없습니다 (드라이버가 설치되고 로드됐나요?)",
  "from the dashboard: {recipes} (built as comfyfarm-build)":
    "대시보드에서: {recipes} (comfyfarm-build 계정으로 빌드)",
  "from the dashboard: allowed, but no recipe is installed — run setup again":
    "대시보드에서: 허용됐지만 설치된 레시피가 없습니다 — 설치를 다시 실행하세요",
  "no profile template in {art} — on a host with internet that mounts it here: python3 publish.py recipe {art} rocm-gfx1201 (docs/guide/rollouts.md §1)":
    "{art}에 프로필 견본이 없습니다 — 인터넷이 되고 이 저장소를 같은 경로로 마운트한 호스트에서: python3 publish.py recipe {art} rocm-gfx1201 (docs/guide/rollouts.md §1)",
  "{path} is not readable — is the store mounted here, at the same path as on the other hosts?":
    "{path}을(를) 읽을 수 없습니다 — 저장소가 다른 호스트와 같은 경로로 여기 마운트돼 있나요?",
  "{path} is not writable by {user}":
    "{user} 계정이 {path}에 쓸 수 없습니다",
  "the host's (a container keeps it); compared with the router's below":
    "호스트의 시계 (컨테이너는 호스트 시계를 씁니다) — 아래에서 라우터 시계와 비교합니다",
  "/dev/kfd present but {user} cannot open it or a render node — add the account to the render and video groups (setup does), then log in again":
    "/dev/kfd가 있지만 {user} 계정이 그것이나 렌더 노드를 열 수 없습니다 — 계정을 render·video 그룹에 넣고(설치가 합니다) 다시 로그인하세요",
  "/dev/nvidiactl present but {user} cannot open it":
    "/dev/nvidiactl이 있지만 {user} 계정이 열 수 없습니다",
  "no GPU found — this host can only run ComfyUI on the CPU":
    "GPU가 없습니다 — 이 호스트는 ComfyUI를 CPU로만 돌릴 수 있습니다",
  "{templates} (templates: System → Rollout → From a recipe makes profiles of them)":
    "{templates} (견본: 시스템 → 롤아웃 → 레시피로 만들기에서 프로필로 만듭니다)",
  "{templates}: their artifacts are missing — build again (System → Rollout → Build)":
    "{templates}: 아티팩트가 없습니다 — 다시 빌드하세요 (시스템 → 롤아웃 → 빌드)",
  "{path}: {n} published artifact(s)":
    "{path}: 게시된 아티팩트 {n}개",
  "{path}: nothing published — publish ComfyUI, a venv and the node set before assigning a profile":
    "{path}: 게시된 것이 없습니다 — 프로필을 할당하기 전에 ComfyUI, venv, 노드 세트를 게시하세요",
  "{path}: {gb} GB free":
    "{path}: {gb} GB 남음",
  "{path}: {gb} GB free — less than {min} GB: copies of ComfyUI and the node sets go here":
    "{path}: {gb} GB 남음 — {min} GB 미만입니다: ComfyUI와 노드 세트 사본이 여기 놓입니다",
  "{path} — missing or not writable by {who}; on shared storage give that uid write access there (every host uses the same uid)":
    "{path} — 없거나 {who}이(가) 쓸 수 없습니다. 공유 저장소라면 그 uid에 쓰기 권한을 주세요 (모든 호스트가 같은 uid를 씁니다)",
  "{path} — missing or not readable: mount the models there (the same on every GPU worker) or make the directory and copy them in":
    "{path} — 없거나 읽을 수 없습니다: 모델을 거기 마운트하거나(모든 GPU 워커에 같게) 디렉터리를 만들고 복사해 넣으세요",
  "Python {version} runs here":
    "Python {version} 실행됨",
  "NTP synchronised":
    "NTP 동기화됨",
  "not synchronised — job times and session expiry compare clocks across hosts":
    "동기화 안 됨 — 작업 시간과 세션 만료는 호스트들의 시계를 서로 비교합니다",
  "{router} answers, clocks {seconds} s apart":
    "{router} 응답함, 시계 차이 {seconds}초",
  "{router} answers":
    "{router} 응답함",
  "{router} does not answer: {error} — firewall, address, or the router is down":
    "{router}이(가) 응답하지 않습니다: {error} — 방화벽, 주소, 또는 라우터가 꺼져 있습니다",
  "{addr} is free":
    "{addr} 비어 있음",
  "{user}: {groups} — missing {missing}":
    "{user}: {groups} — {missing} 없음",
  "{user}: {groups}":
    "{user}: {groups}",
  "{n} GPU(s): {targets} · amdgpu {version}":
    "GPU {n}개: {targets} · amdgpu {version}",
  "{n} GPU(s): {targets}":
    "GPU {n}개: {targets}",
  "nvidia-smi failed: {error} — driver not loaded?":
    "nvidia-smi 실패: {error} — 드라이버가 로드되지 않았나요?",
  "{path} is empty — ComfyUI will list no models and every job that loads one fails: put them in its subdirectories as in ComfyUI's own models directory (checkpoints/, loras/, vae/, …)":
    "{path}이(가) 비어 있습니다 — ComfyUI에 모델이 하나도 안 보이고, 모델을 불러오는 작업은 모두 실패합니다: ComfyUI의 models 디렉터리처럼 하위 디렉터리(checkpoints/, loras/, vae/ …)에 넣으세요",
  "{path} overlaps <ART> or the code under <LOCAL> — the launcher will refuse every render (contract §4)":
    "{path}이(가) <ART>나 <LOCAL> 아래 코드와 겹칩니다 — 런처가 모든 render를 거부합니다 (계약 §4)",
  "torch cannot use this host's GPU: {error} — the NVIDIA driver must be new enough for that CUDA (nvidia-smi shows the highest it supports): update the driver, or use a recipe built for an older CUDA":
    "torch가 이 호스트의 GPU를 쓰지 못합니다: {error} — NVIDIA 드라이버가 그 CUDA를 지원할 만큼 새로워야 합니다(nvidia-smi가 지원하는 최고 버전을 보여줍니다): 드라이버를 올리거나 더 낮은 CUDA용 레시피를 쓰세요",
  "torch cannot use this host's GPU: {error}":
    "torch가 이 호스트의 GPU를 쓰지 못합니다: {error}",
  "a system library is missing on this host: {lib} — install {pkg} from your local mirror":
    "이 호스트에 시스템 라이브러리가 없습니다: {lib} — 로컬 미러에서 {pkg}를 설치하세요",
  "a system library is missing on this host: {lib} — install the package that provides it":
    "이 호스트에 시스템 라이브러리가 없습니다: {lib} — 이를 제공하는 패키지를 설치하세요",
  "its interpreter is not on this host ({path}) — a venv built before D-42 points into the build host; publish it again":
    "인터프리터가 이 호스트에 없습니다 ({path}) — D-42 이전에 빌드한 venv는 빌드 호스트를 가리킵니다. 다시 게시하세요",
  "does not run here: {error}":
    "여기서 실행되지 않습니다: {error}",
  "{seconds} s off the router's clock":
    "라우터 시계와 {seconds}초 차이",
  "check failed: {error}":
    "점검 실패: {error}",
  "{addr} is in use by this instance":
    "{addr}을(를) 이 인스턴스가 쓰는 중",
  "{addr} is in use by something else — pick another port for the instance":
    "{addr}을(를) 다른 것이 쓰는 중 — 인스턴스에 다른 포트를 고르세요",
  "copies":
    "복사본",
  "checked against the builder's key ({path})":
    "빌더의 키로 검증 ({path})",
  "{path} is missing: copies cannot be checked — set the host up again":
    "{path}이(가) 없습니다: 복사본을 검증할 수 없습니다 — 호스트를 다시 설치하세요",
  "comfyfarm-fetch.path takes the requests":
    "comfyfarm-fetch.path가 요청을 받습니다",
  "comfyfarm-fetch.path is {state}: nothing makes the copies — sudo systemctl enable --now comfyfarm-fetch.path":
    "comfyfarm-fetch.path가 {state} 상태입니다: 복사본을 만드는 것이 없습니다 — sudo systemctl enable --now comfyfarm-fetch.path",
  "{path}: copies of the control plane's artifacts — {n} here; the ones a profile needs are copied before it starts":
    "{path}: 제어 영역 아티팩트의 복사본 — 지금 {n}개. 프로필에 필요한 것은 시작 전에 복사됩니다",
  "/dev/kfd present but {user} cannot open it — not needed: this worker runs on the CPU":
    "/dev/kfd가 있지만 {user} 계정이 열 수 없습니다 — 필요 없습니다: 이 워커는 CPU로 돕니다",
  "model store":
    "모델 저장소",
  "{path} is missing or not readable — set the host up again":
    "{path}이(가) 없거나 읽을 수 없습니다 — 호스트를 다시 설치하세요",
  "{path}: copies of the models this worker's group uses — {n} here, {gb} GB free; new ones are copied in the background":
    "{path}: 이 워커 그룹이 쓰는 모델의 복사본 — 지금 {n}개, 여유 {gb} GB. 새 모델은 백그라운드로 복사됩니다",
  "{path}: the farm's models, used as they are (a share, or the control plane's own store)":
    "{path}: 팜의 모델을 그대로 씁니다 (공유 폴더, 또는 제어 영역 자체의 저장소)",
  "{path} is missing — mount the control plane's model store there, or the models added in the dashboard do not reach this worker":
    "{path}이(가) 없습니다 — 제어 영역의 모델 저장소를 거기에 마운트하세요. 아니면 대시보드에서 추가한 모델이 이 워커에 닿지 않습니다",
  // ---- System → Models (docs/design/17) ----
  "Models": "모델", "Model": "모델", "Source": "출처", "Size": "크기", "Copies": "복사본", "Copying": "복사 중",
  "Problems": "문제", "Add models": "모델 추가", "Download": "다운로드", "Asked again": "다시 요청했습니다",
  "ready": "준비됨", "downloading": "다운로드 중",
  "Copies on the GPU workers": "GPU 워커의 복사본",
  "Each worker gets the models its group uses, in the background — no restart. ComfyUI lists a new one when its model list is refreshed.":
    "각 워커는 자기 그룹이 쓰는 모델을 백그라운드로 받습니다 — 재시작 없음. 새 모델은 ComfyUI의 모델 목록을 새로 고치면 보입니다.",
  "No models yet. Add Hugging Face links — the builder downloads them, GPU workers get copies.":
    "아직 모델이 없습니다. Hugging Face 링크를 추가하세요 — 빌더가 내려받고, GPU 워커가 복사본을 받습니다.",
  "Groups that use them": "사용하는 그룹",
  "their GPU workers get a copy; none checked: kept in the store only": "그 그룹의 GPU 워커가 복사본을 받습니다. 아무것도 고르지 않으면 저장소에만 둡니다",
  "Hugging Face file links, one per line": "Hugging Face 파일 링크 (한 줄에 하나)",
  "as copied from the file's page; a line may start with its folder: loras https://huggingface.co/…":
    "파일 페이지에서 복사한 그대로. 줄 앞에 폴더를 붙여도 됩니다: loras https://huggingface.co/…",
  "Folder in ComfyUI": "ComfyUI 폴더",
  "from the link (…/split_files/vae/… → vae)": "링크에서 (…/split_files/vae/… → vae)",
  "no links": "링크가 없습니다",
  "no folder in the link — pick one, or start the line with it: {line}": "링크에 폴더가 없습니다 — 폴더를 고르거나 줄 앞에 붙이세요: {line}",
  "{n} model(s) asked of the builder — it downloads them one after another": "모델 {n}개를 빌더에 요청했습니다 — 하나씩 차례로 내려받습니다",
  "Remove {name} from the list? It never arrived: nothing is in the store or on a worker.":
    "{name}을(를) 목록에서 지울까요? 받아지지 않은 모델이라 저장소에도 워커에도 아무것도 없습니다.",
  "(none — in the store only)": "(없음 — 저장소에만)",
  "no model store on this host — Set up again (System → Workers) to have one": "이 호스트에는 모델 저장소가 없습니다 — 쓰려면 다시 설치하세요 (시스템 → 워커)",
  "uses the store as it is": "저장소를 그대로 사용",
  "no control-plane builder here (the compose deployment's builder service)": "여기에는 제어 영역 빌더가 없습니다 (compose 배포의 builder 서비스)",
  "no control-plane builder here: models from the dashboard need the compose deployment's builder":
    "여기에는 제어 영역 빌더가 없습니다: 대시보드 모델에는 compose 배포의 빌더가 필요합니다",
  "the builder is not running (docker compose ps builder; its log says why)": "빌더가 실행 중이 아닙니다 (docker compose ps builder — 이유는 빌더 로그에 있습니다)",
  "the builder has no model store (COMFYFARM_MODELS, docs/guide/models.md)": "빌더에 모델 저장소가 없습니다 (COMFYFARM_MODELS, docs/guide/models.md)",
  "the builder has no model store: set COMFYFARM_MODELS_MOUNT (docs/guide/models.md)": "빌더에 모델 저장소가 없습니다: COMFYFARM_MODELS_MOUNT를 설정하세요 (docs/guide/models.md)",
  "the router does not see the model store (COMFYFARM_MODELS_DIR): GPU workers cannot copy from it":
    "라우터가 모델 저장소를 보지 못합니다 (COMFYFARM_MODELS_DIR): GPU 워커가 복사해 갈 수 없습니다",
  "Formats: {formats} — others can run code when ComfyUI loads them.": "형식: {formats} — 다른 형식은 ComfyUI가 불러올 때 코드를 실행할 수 있습니다.",
  "All formats allowed (COMFYFARM_MODELS_ALLOW_PICKLE=1).": "모든 형식 허용 (COMFYFARM_MODELS_ALLOW_PICKLE=1).",
  "A Hugging Face token is set for gated models.": "게이트 모델용 Hugging Face 토큰이 설정되어 있습니다.",
  "No Hugging Face token: gated models fail until one is in the builder's hf_token file.":
    "Hugging Face 토큰이 없습니다: 게이트 모델은 빌더의 hf_token 파일에 토큰을 넣기 전까지 실패합니다.",
  // ---- uploads (docs/design/18) ----
  "Upload": "업로드", "Upload all": "모두 업로드", "Uploads": "업로드", "Clear": "비우기", "Go on": "이어 올리기",
  "Remove from the list": "목록에서 빼기", "pick the same file again": "같은 파일을 다시 고르세요",
  "staged": "대기", "uploading": "올리는 중",
  "each file goes up in pieces; a broken connection goes on where it stopped": "파일은 조각으로 올라갑니다 — 연결이 끊기면 끊긴 곳부터 이어집니다",
  "{done} of {total} arrived — stopped": "{total} 중 {done} 도착 — 멈춤",
  "not the same file: {name}, {size} bytes": "같은 파일이 아닙니다: {name}, {size}바이트",
  "the builder is not running, or has no model store (docs/guide/models.md)": "빌더가 실행 중이 아니거나 모델 저장소가 없습니다 (docs/guide/models.md)",
  "the router cannot write the model store's .uploads directory (docs/guide/models.md)": "라우터가 모델 저장소의 .uploads 디렉터리에 쓸 수 없습니다 (docs/guide/models.md)",
  "uploaded by {who}": "{who} 업로드", "from the inbox": "인박스에서",
  "Scan the inbox": "인박스 확인", "Scanning the inbox…": "인박스 확인 중…",
  "Inbox: files copied (as root) into {path}/<folder>/ on the control-plane host, then Scan the inbox.":
    "인박스: 제어 영역 호스트의 {path}/<폴더>/에 (root로) 파일을 복사한 뒤 '인박스 확인'을 누르세요.",
  "The builder takes in what the inbox holds — the models appear below": "빌더가 인박스의 파일을 들여옵니다 — 아래 목록에 나타납니다",
  "Last scan: {n} taken in": "마지막 확인: {n}개 들여옴", "{n} left in the inbox": "{n}개는 인박스에 남음",
  "Last scan failed: {error}": "마지막 확인 실패: {error}",
  "Upload an archive": "아카이브 업로드", "Node pack from {file}": "{file}의 노드팩",
  "Name (its directory under custom_nodes)": "이름 (custom_nodes 아래 디렉터리)",
  "archive: {file}": "아카이브: {file}",
  "{name} is in — Build to put it into the artifact store": "{name}을(를) 받았습니다 — 빌드하면 아티팩트 저장소에 들어갑니다",
  "this farm's builder takes no node pack archives (its COMFYFARM_BUILD_NODE_ARCHIVES is off)": "이 팜의 빌더는 노드팩 아카이브를 받지 않습니다 (COMFYFARM_BUILD_NODE_ARCHIVES 꺼짐)",
  "a git pack: the newest commit of its branch or tag; an archive pack: upload its new version": "git 노드팩: 브랜치·태그의 최신 커밋 · 아카이브 노드팩: 새 버전 업로드",
  "Select all": "모두 선택", "{n} selected": "{n}개 선택", "Clear selection": "선택 해제", "Scan": "확인",
  "Add to groups": "그룹에 추가", "Take out of groups": "그룹에서 빼기", "Take out": "빼기",
  "Add {n} model(s) to groups": "모델 {n}개를 그룹에 추가", "Take {n} model(s) out of groups": "모델 {n}개를 그룹에서 빼기",
  "{n} model(s) added to groups": "모델 {n}개를 그룹에 추가했습니다", "{n} model(s) taken out of groups": "모델 {n}개를 그룹에서 뺐습니다",
  "their GPU workers get copies in the background": "그 그룹의 GPU 워커가 백그라운드로 복사본을 받습니다",
  "every model this scan takes in goes into these groups; none checked: no groups yet": "이번에 들여오는 모델 전부가 이 그룹에 들어갑니다. 아무것도 고르지 않으면 그룹 없이 들어옵니다",
  // ---- model folders, deleting models (docs/design/17 phase 2) ----
  "Folders": "폴더", "Model folders": "모델 폴더", "ComfyUI's own": "ComfyUI 기본 폴더", "This farm's (ticked: removed)": "이 팜의 폴더 (체크하면 삭제)",
  "A new folder": "새 폴더",
  "the name a custom node looks its models up by (lowercase letters, digits, _ . -). Workers set up since 0.1.0-dev.19 list a new folder after their next start (a rollout or a restart)":
    "커스텀 노드가 모델을 찾는 폴더 이름 (소문자·숫자·_ . -). 0.1.0-dev.19 이후 설치한 워커는 다음 시작(롤아웃·재시작) 뒤 새 폴더를 봅니다",
  "Saved — workers see a new folder after their next start": "저장했습니다 — 워커는 다음 시작 뒤 새 폴더를 봅니다",
  "search models": "모델 검색", "{n} model(s)": "모델 {n}개",
  "Delete {name} from the farm? It goes from the model store; {where}. Queued jobs that use it will fail.":
    "{name}을(를) 팜에서 삭제할까요? 모델 저장소에서 지워지고, {where}. 이 모델을 쓰는 대기 중 작업은 실패합니다.",
  "the copies on the workers of {groups} go too": "{groups} 그룹 워커의 복사본도 지워집니다", "no group uses it": "쓰는 그룹은 없습니다",
  "Deleting — the workers drop their copies in the background": "삭제 중 — 워커의 복사본은 백그라운드로 지워집니다",
  "Saved — the workers of {groups} drop their copies": "저장했습니다 — {groups} 그룹 워커의 복사본은 지워집니다",
  "the workers of those groups drop their copies": "그 그룹 워커의 복사본은 지워집니다",
  "removing {n}": "{n}개 지우는 중", "deleting": "삭제 중",
  "older worker tools: no new folders, copies are not removed — Set up again (System → Workers)":
    "이전 워커 도구: 새 폴더를 못 쓰고 복사본도 지워지지 않습니다 — 다시 설치하세요 (시스템 → 워커)",
  "model folder added": "모델 폴더 추가", "model folder removed": "모델 폴더 삭제", "model deleted": "모델 삭제",
  "models' groups changed": "모델 그룹 변경", "model uploaded": "모델 업로드", "model inbox scanned": "인박스 확인",
  // ---- audit actions (the router's fixed words) ----
  "comfyfarm (first start)": "comfyfarm (첫 시작)", "assign": "할당", "build requested": "빌드 요청",
  "first administrator created": "첫 관리자 생성", "instance configured": "인스턴스 설정", "instance removed": "인스턴스 삭제",
  "instance started": "인스턴스 시작", "maintenance off": "점검 끝", "maintenance on": "점검 시작",
  "node pack added": "노드팩 추가", "node pack changed": "노드팩 변경", "node pack removed": "노드팩 삭제",
  "model added": "모델 추가", "model changed": "모델 변경", "model retried": "모델 다시 받기", "model removed": "모델 삭제",
  "profile changed": "프로필 변경", "profile created": "프로필 생성", "profile deleted": "프로필 삭제",
  "rollout abort": "롤아웃 중단", "rollout resumed": "롤아웃 재개", "rollout started": "롤아웃 시작",
  "worker setup created": "워커 설치 생성", "worker setup finished": "워커 설치 완료", "worker setup over ssh": "ssh로 워커 설치",
  "worker setup renewed": "워커 설치 명령 갱신", "worker setup replaced": "워커 설치 대체", "worker changed": "워커 변경",
  "setting changed": "설정 변경", "rollout succeeded": "롤아웃 성공", "rollout failed": "롤아웃 실패",
  "rollout aborted": "롤아웃 중단됨", "worker setup reported": "워커 설치 보고", "UI worker container added": "UI 워커 컨테이너 추가",
  "worker setup": "워커 설치",
  "(the host has reported — no need to run the script again)": "(호스트가 이미 보고했습니다 — 스크립트를 다시 실행할 필요 없음)",
  "http://<router address>:<router port> — the address the launcher fetches its render from": "http://<라우터 주소>:<라우터 포트> — 런처가 렌더를 받아 가는 주소",
};
