# Task Processing Flow

This document explains how a task moves through the system from creation to completion.

The goal is to make the flow understandable even for someone who is still getting comfortable with background workers, polling, and asynchronous processing.

## Big Picture

The task mechanism is split into four parts:

1. API layer
   Receives requests such as "create task".
2. Storage layer
   Saves the task and its current state such as `queued`, `running`, or `completed`.
3. Poller and worker pool
   Repeatedly checks storage for queued tasks and assigns them to free workers.
4. Executor and mock worker logic
   Performs the actual mock backup or restore work and reports progress.

The most important architectural idea is this:

- Creating a task does not execute it immediately.
- Creating a task only stores it as `queued`.
- A background poller later discovers that queued task.
- A free worker thread takes the task and executes it.
- Progress is written back to storage while the task is still running.

## 1. Server Startup

The application starts in [backup_server.py](/path/to/backup-server/python/srv/backup_server.py).

The method `lifespan()` prepares the application runtime.

It does the following:

- loads hypervisors into memory
- loads jobs into memory
- loads tasks into memory
- loads discovered resources into memory
- creates the task pool
- starts the task pool

Important lines:

- [backup_server.py](/path/to/backup-server/python/srv/backup_server.py#L20)
- [backup_server.py](/path/to/backup-server/python/srv/backup_server.py#L31)

You can think of this as opening the workshop in the morning:

- existing records are loaded
- workers arrive
- dispatcher starts watching the queue

## 2. Creating a Task

Task creation happens in [tasks.py](/path/to/backup-server/python/srv/api/tasks.py).

The main method is:

- `create_task()` at [tasks.py](/path/to/backup-server/python/srv/api/tasks.py#L12)

When the API receives `POST /task`, this method:

- verifies the hypervisor exists
- verifies the job exists, if one was provided
- verifies the referenced resource was discovered already
- creates a task record
- sets `status` to `queued`
- stores the task using `persist_task()`

Important detail:

- this method does not execute the task directly
- it only records the task as work waiting to be done

This is like writing a work ticket and placing it into the inbox.

## 3. Persisting Task State

Task persistence is handled in [mongo_db_wrapper.py](/path/to/backup-server/python/srv/core/mongo_db_wrapper.py).

Important methods:

- `persist_task()` at [mongo_db_wrapper.py](/path/to/backup-server/python/srv/core/mongo_db_wrapper.py#L115)
- `load_queued_tasks()` at [mongo_db_wrapper.py](/path/to/backup-server/python/srv/core/mongo_db_wrapper.py#L123)
- `claim_task()` at [mongo_db_wrapper.py](/path/to/backup-server/python/srv/core/mongo_db_wrapper.py#L147)

### `persist_task()`

This method:

- normalizes the task record
- saves it in the in-memory `TASKS` dictionary
- writes it to MongoDB if database persistence is enabled

### `load_queued_tasks()`

This method:

- finds tasks that are still waiting with status `queued`
- returns them in creation order

### `claim_task()`

This method:

- changes a task from `queued` to `running`
- prevents the same task from being taken twice

This is one of the most important safety points in the design.

Without claiming, two workers might accidentally pick the same task.

## 4. MongoDB Support

If MongoDB is enabled, the lower-level database logic is in [db_manager.py](/path/to/backup-server/python/srv/db/db_manager.py).

Important methods:

- `get_queued_tasks()` at [db_manager.py](/path/to/backup-server/python/srv/db/db_manager.py#L108)
- `claim_task()` at [db_manager.py](/path/to/backup-server/python/srv/db/db_manager.py#L117)

The database `claim_task()` method only succeeds if the task is still `queued`.

This means only one worker can claim it successfully.

That makes the polling model safe even when the dispatcher checks frequently.

## 5. Poller and Worker Pool

The orchestration logic is in [task_pool.py](/path/to/backup-server/python/srv/core/task_pool.py).

The main class is:

- `TaskProcessPool` at [task_pool.py](/path/to/backup-server/python/srv/core/task_pool.py#L10)

Despite the name, the current implementation uses a `ThreadPoolExecutor`.

That means:

- one process
- multiple worker threads

### `start()`

At [task_pool.py](/path/to/backup-server/python/srv/core/task_pool.py#L28)

This method:

- creates the worker thread pool
- starts a background poller thread

### `_poll_loop()`

At [task_pool.py](/path/to/backup-server/python/srv/core/task_pool.py#L55)

This loop runs continuously:

- check whether workers are free
- if workers are free, check for queued tasks
- dispatch queued tasks to available workers
- sleep briefly
- repeat

This is like a dispatcher standing beside the queue and repeatedly asking:

- "Which workers are free?"
- "Is there work waiting?"

### `_dispatch_available_work()`

At [task_pool.py](/path/to/backup-server/python/srv/core/task_pool.py#L61)

This method:

- calculates available worker slots
- loads queued tasks up to that limit
- claims each task
- submits each claimed task to a worker

Example:

- worker count is 4
- 6 tasks are queued
- 4 tasks start running
- 2 tasks remain queued

That is exactly the behavior shown in the test output.

### `_submit_claimed_task()`

At [task_pool.py](/path/to/backup-server/python/srv/core/task_pool.py#L78)

This method sends the task to a worker thread by calling `run_task(...)`.

It also passes a progress callback so the worker can report live updates while it runs.

### `_report_progress()`

At [task_pool.py](/path/to/backup-server/python/srv/core/task_pool.py#L93)

When the worker reports progress, this method:

- keeps the task status as `running`
- merges the progress data into the task result
- updates the timestamp
- persists the updated task

This is why the CLI and tests can show:

- stage name
- progress percentage
- process id
- thread id
- thread name
- progress message

### `_complete_task()`

At [task_pool.py](/path/to/backup-server/python/srv/core/task_pool.py#L105)

When the worker finishes:

- final result is collected
- task becomes `completed` or `failed`
- final result is stored

This closes the task lifecycle.

## 6. Executing the Task

Execution routing is handled in [task_executor.py](/path/to/backup-server/python/srv/core/task_executor.py).

Main method:

- `run_task()` at [task_executor.py](/path/to/backup-server/python/srv/core/task_executor.py#L16)

This method:

- checks the task snapshot is executable
- detects whether this is a backup or restore task
- routes the task into the mock worker logic
- adds final metadata such as `finished_at`
- returns the completion result

You can think of this layer as a foreman:

- it does not do the heavy work itself
- it decides which execution path should handle the task

## 7. Mock Backup and Restore Logic

The actual mock work is implemented in [mock_backup_emulator.py](/path/to/backup-server/python/srv/services/mock_backup_emulator.py).

Main class:

- `MockBackupEmulator` at [mock_backup_emulator.py](/path/to/backup-server/python/srv/services/mock_backup_emulator.py#L9)

Main methods:

- `backup()` at [mock_backup_emulator.py](/path/to/backup-server/python/srv/services/mock_backup_emulator.py#L18)
- `restore()` at [mock_backup_emulator.py](/path/to/backup-server/python/srv/services/mock_backup_emulator.py#L25)
- `_run()` at [mock_backup_emulator.py](/path/to/backup-server/python/srv/services/mock_backup_emulator.py#L32)

### `_run()`

This method:

- decides how long the mock work will take
- reads the current process id
- reads the current thread id and thread name
- builds a sequence of work stages
- reports progress at each stage
- sleeps between stages to simulate real work
- returns the final completion result

For backup tasks the stages are:

- prepare
- snapshot
- transfer
- verify
- finalize

For restore tasks the stages are:

- prepare
- fetch
- apply
- verify
- finalize

This makes the task look and behave like a real long-running operation instead of just sleeping silently.

## 8. CLI Progress Watching

The CLI polling logic is in [main.py](/path/to/backup-server/python/cli/main.py).

Important method:

- `watch_task()` at [main.py](/path/to/backup-server/python/cli/main.py#L290)

This method:

- polls `GET /tasks/{task_id}`
- compares the current response with the previous response
- prints whenever task result data changes
- exits when the task reaches `completed` or `failed`

So the CLI does not push progress.

It simply keeps asking storage for the latest task record and prints new changes.

## 9. What Parallel Execution Means Here

At the moment, tasks are executed in parallel by threads, not by separate processes.

That means:

- all concurrent tasks share the same process id
- concurrent tasks have different thread ids and thread names

This is why the verbose trace shows:

- same `worker_pid`
- different `task-worker_0`, `task-worker_1`, `task-worker_2`, `task-worker_3`

So yes, the tasks are running in parallel, but inside one process.

A simple analogy:

- one office building = one process
- several employees at different desks = worker threads
- dispatcher gives each free employee the next work ticket

## 10. End-to-End Lifecycle Summary

From start to finish, the flow looks like this:

1. client sends `POST /task`
2. `create_task()` validates and creates a queued task
3. `persist_task()` saves it
4. poller loop wakes up
5. poller asks how many worker slots are free
6. poller loads queued tasks
7. poller claims tasks safely
8. poller submits claimed tasks to worker threads
9. `run_task()` routes the task to backup or restore logic
10. `MockBackupEmulator` reports progress stage by stage
11. `_report_progress()` writes progress back into task storage
12. worker finishes execution
13. `_complete_task()` marks the task as completed
14. CLI or tests read the updated task and display the result

## 11. Why This Separation Is Useful

The architecture is easier to understand because responsibilities are separated clearly:

- API accepts work
- storage remembers work state
- poller dispatches work
- workers execute work
- CLI observes work

That separation makes the system:

- easier to debug
- easier to test
- easier to extend later

For example, later you could:

- replace the mock emulator with a real backup implementation
- switch from threads to processes
- keep the same task creation API
- keep the same queue and polling model

## 12. Practical Mental Model

If you want one simple picture in your mind, use this:

`Client -> API -> Storage(queue) -> Poller -> Worker -> Storage(result) -> CLI/Test output`

That is the full task processing story in one line.
