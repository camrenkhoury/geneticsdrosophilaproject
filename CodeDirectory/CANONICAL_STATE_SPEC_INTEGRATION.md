# Canonical State Spec Integration Notes

This project already implements part of the state model locally inside the Tkinter GUI, but it does not yet implement the full backend/client split described in the canonical state spec.

## Current Reality

Current codebase shape:

- `gui.py`
  - Owns the Tkinter client UI
  - Owns the current in-process task orchestration
  - Owns stop handling through `stop_requested`
  - Owns actuator status mirroring to the UI queue
  - Implements local machine workflows directly
- `motion.py`
  - Owns homing/move behavior
- `vacuum.py`
  - Owns vacuum control
- `vibration.py`
  - Owns vibration control
- `assay.py`
  - Owns assay logic
- `fly_classifier.py`
  - Owns classify logic
- `gantryOperation.py`
  - Separate standalone automated workflow script
- `nozzle_implementation.py`
  - Alternate standalone workflow implementation

What is missing relative to the canonical spec:

- No FastAPI backend
- No `/health` or `/status` API
- No explicit `LocalController` / `RemoteController` abstraction
- No shared runtime state store object used across backend and GUI
- No systemd-managed backend process layer in this repository
- No explicit lifecycle enums for backend/client/task states

## What Already Matches the Spec

The current GUI already aligns with several rules:

- One active machine task at a time
  - enforced by `worker_thread` checks in `gui.py`
- Global stop flag
  - `stop_requested` already exists and is checked by workers
- Safe actuator defaults on task unwind
  - vacuum/vibration are turned off in worker cleanup paths
- Homing belongs to machine operations
  - homing is triggered from automation/task logic, not from UI state transitions
- Automated workflow belongs to machine operations/task workflow
  - implemented in `_run_automated_worker()`

## Main Architectural Gap

Right now the GUI is both:

- the client
- the orchestrator
- the runtime state store
- the local machine controller

The canonical spec wants those concerns separated into:

1. backend lifecycle layer
2. client session layer
3. command/task orchestrator layer
4. hardware/controller layer

That means the next real step is not "add more buttons." It is introducing a state model and controller boundary.

## Recommended Implementation Order

### Phase 1: Formalize local runtime state

Add a central runtime state model first, without introducing networking yet.

Recommended new modules:

- `state_models.py`
- `runtime_state.py`
- `task_enums.py`

Minimum state to centralize:

- backend/client mode
- orchestrator state
- active task state
- current task name
- current position
- vacuum enabled
- vibration enabled
- stop requested
- latest message
- health flags
- last classification result
- last detection metadata

This phase should refactor `gui.py` to read/write the runtime state object rather than treating widget variables as the system truth.

### Phase 2: Introduce controller abstraction

Add:

- `controllers/base.py`
- `controllers/local_controller.py`
- `controllers/remote_controller.py`

`LocalController` should wrap current direct calls:

- `home_to_zero`
- `move_to_absolute`
- `vacuum_on` / `vacuum_off`
- `vibration_on` / `vibration_off`
- `assay`
- `classify_fly`
- automated workflow entrypoint

The GUI should only talk to a controller interface, not directly to motion/vacuum/vibration modules.

### Phase 3: Extract task orchestrator from GUI

Move worker logic out of `gui.py` into something like:

- `task_orchestrator.py`
- `machine_tasks.py`

That layer should own:

- validation
- task transitions
- machine lock ownership
- stop handling
- task outcome states

At that point the state spec becomes implementable instead of aspirational.

### Phase 4: Add backend process and remote API

Only after the local state/orchestrator/controller split is clean:

- add FastAPI backend
- expose `/health`
- expose `/status`
- expose command endpoints
- keep systemd ownership outside GUI logic

Recommended new modules:

- `backend/app.py`
- `backend/routes.py`
- `backend/dependencies.py`

### Phase 5: Add reconnecting remote GUI mode

Then wire GUI states for:

- `REMOTE_CONTROLLER_MODE`
- `CONNECTING_TO_PI`
- `CLIENT_CONNECTED`
- `CLIENT_DISCONNECTED`
- `RETRY_WAIT`
- `RECONNECT_ATTEMPT`
- `CLIENT_RECONNECTED`

## Concrete Mapping to Current `gui.py`

Current `gui.py` sections roughly map like this:

- Entry/landing UI
  - `GUI_STARTING`
  - `ENTRY_PAGE_ACTIVE`
- Main panel
  - `CONTROL_PANEL_ACTIVE`
- Worker thread ownership
  - partial `SYSTEM_IDLE`
  - partial `TASK_STARTING`
  - partial active task states
- `_run_automated_worker()`
  - partial `AUTO_*` chain
- `_run_assay_worker()`
  - partial `ASSAY_RUNNING`
- `_classify_worker()`
  - partial `CLASSIFY_RUNNING`
- `stop_requested`
  - partial `STOP_REQUESTED`
  - partial `TASK_STOPPING`

What it does not model explicitly:

- `TASK_VALIDATING`
- `COMMAND_REJECTED`
- `TASK_COMPLETE`
- `TASK_ERROR`
- detailed task outcome enums
- backend lifecycle states
- remote connection states

## Recommended Enum Sets for This Repo

If implemented now, the smallest useful enums would be:

### `OrchestratorState`

- `SYSTEM_IDLE`
- `TASK_VALIDATING`
- `TASK_STARTING`
- `ACTUATOR_APPLYING`
- `TASK_STOPPING`
- `TASK_COMPLETE`
- `TASK_ERROR`

### `TaskState`

- `HOMING_RUNNING`
- `HOMING_COMPLETE`
- `HOMING_STOPPED`
- `HOMING_ERROR`
- `MOVE_RUNNING`
- `MOVE_COMPLETE`
- `MOVE_LIMITED_OR_BLOCKED`
- `MOVE_STOPPED`
- `MOVE_ERROR`
- `ASSAY_RUNNING`
- `ASSAY_COMPLETE`
- `ASSAY_STOPPED`
- `ASSAY_ERROR`
- `CLASSIFY_RUNNING`
- `CLASSIFY_COMPLETE`
- `CLASSIFY_STOPPED`
- `CLASSIFY_ERROR`
- `AUTO_PRECHECK`
- `AUTO_CYCLE_START`
- `AUTO_HOME_CYCLE_START`
- `AUTO_MOVE_TO_PHOTO_POSITION`
- `AUTO_WAIT_FOR_DETECTION`
- `AUTO_NO_FLIES_REMAINING`
- `AUTO_DETECTION_RETRY_WAIT`
- `AUTO_SELECT_PICKUP`
- `AUTO_HOME_ACCURACY_RESET`
- `AUTO_MOVE_TO_PICKUP`
- `AUTO_PICKUP_FLY`
- `AUTO_MOVE_TO_CHAMBER`
- `AUTO_DROP_IN_CHAMBER`
- `AUTO_IDENTIFICATION_WINDOW`
- `AUTO_REPICK_FROM_CHAMBER`
- `AUTO_SELECT_TUBE`
- `AUTO_MOVE_TO_TUBE`
- `AUTO_DROP_IN_TUBE`
- `AUTO_RETURN_HOME`
- `AUTO_ASSAY_COUNTDOWN`
- `AUTO_ASSAY_RUNNING`
- `AUTOMATED_COMPLETE`
- `AUTOMATED_STOPPED`
- `AUTOMATED_ERROR`

### `ClientSessionState`

For the current local-only app:

- `GUI_CLOSED`
- `GUI_STARTING`
- `ENTRY_PAGE_ACTIVE`
- `CONTROL_PANEL_ACTIVE`
- `LOCAL_CONTROLLER_MODE`
- `GUI_EXITING`

Only add the remote states once the API exists.

## Hard Recommendation

Do not try to implement the entire canonical spec directly inside `gui.py`.

That would make the file worse and would not actually produce the backend/client separation the spec expects.

The correct move is:

1. extract runtime state
2. extract controller interface
3. extract task orchestrator
4. then add backend/API
5. then add remote reconnect behavior

## Best Next Build Step

The next highest-value implementation step is:

- create shared enums + runtime state model
- refactor `gui.py` to use that state model
- keep all behavior local for now

That gives a clean foundation for the rest of the canonical spec without forcing premature network/backend work.
