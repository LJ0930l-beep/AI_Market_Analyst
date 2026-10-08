//! Exact process ownership for the packaged Windows sidecar.
//!
//! PyInstaller one-file starts a second process.  Keeping the launcher handle
//! and assigning both verified processes to one Job Object lets Tauri detect
//! launcher exit independently of HTTP health and retire the worker safely.

#[cfg(target_os = "windows")]
mod platform {
    use std::ffi::{c_void, OsString};
    use std::mem::{size_of, zeroed};
    use std::os::windows::ffi::OsStringExt;
    use std::path::Path;
    use std::ptr;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Mutex;
    use std::thread;
    use std::time::{Duration, Instant};

    type Handle = *mut c_void;
    const PROCESS_TERMINATE: u32 = 0x0001;
    const PROCESS_SET_QUOTA: u32 = 0x0100;
    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const SYNCHRONIZE: u32 = 0x0010_0000;
    const TH32CS_SNAPPROCESS: u32 = 0x0000_0002;
    const WAIT_TIMEOUT: u32 = 258;
    const JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION: i32 = 1;
    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: i32 = 9;
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x2000;

    #[repr(C)]
    struct ProcessEntry32W {
        size: u32,
        usage_count: u32,
        process_id: u32,
        default_heap_id: usize,
        module_id: u32,
        thread_count: u32,
        parent_process_id: u32,
        base_priority: i32,
        flags: u32,
        executable_name: [u16; 260],
    }

    #[repr(C)]
    struct BasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    struct IoCounters {
        read_operation_count: u64,
        write_operation_count: u64,
        other_operation_count: u64,
        read_transfer_count: u64,
        write_transfer_count: u64,
        other_transfer_count: u64,
    }

    #[repr(C)]
    struct ExtendedLimitInformation {
        basic_limit_information: BasicLimitInformation,
        io_info: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    #[repr(C)]
    struct BasicAccountingInformation {
        total_user_time: i64,
        total_kernel_time: i64,
        this_period_total_user_time: i64,
        this_period_total_kernel_time: i64,
        total_page_faults: u32,
        total_processes: u32,
        active_processes: u32,
        total_terminated_processes: u32,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, process_id: u32) -> Handle;
        fn CloseHandle(handle: Handle) -> i32;
        fn WaitForSingleObject(handle: Handle, milliseconds: u32) -> u32;
        fn QueryFullProcessImageNameW(handle: Handle, flags: u32, path: *mut u16, size: *mut u32) -> i32;
        fn CreateToolhelp32Snapshot(flags: u32, process_id: u32) -> Handle;
        fn Process32FirstW(snapshot: Handle, entry: *mut ProcessEntry32W) -> i32;
        fn Process32NextW(snapshot: Handle, entry: *mut ProcessEntry32W) -> i32;
        fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
        fn SetInformationJobObject(job: Handle, class: i32, info: *const c_void, size: u32) -> i32;
        fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
        fn IsProcessInJob(process: Handle, job: Handle, result: *mut i32) -> i32;
        fn TerminateJobObject(job: Handle, exit_code: u32) -> i32;
        fn QueryInformationJobObject(job: Handle, class: i32, info: *mut c_void, size: u32, returned: *mut u32) -> i32;
    }

    fn parent_pid(pid: u32) -> Option<u32> {
        let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
        if snapshot.is_null() || snapshot == (-1_isize) as Handle {
            return None;
        }
        let mut entry: ProcessEntry32W = unsafe { zeroed() };
        entry.size = size_of::<ProcessEntry32W>() as u32;
        let mut found = None;
        let mut more = unsafe { Process32FirstW(snapshot, &mut entry) } != 0;
        while more {
            if entry.process_id == pid {
                found = Some(entry.parent_process_id);
                break;
            }
            more = unsafe { Process32NextW(snapshot, &mut entry) } != 0;
        }
        unsafe { CloseHandle(snapshot); }
        found
    }

    fn image_path(handle: Handle) -> Result<String, String> {
        let mut buffer = vec![0_u16; 32_768];
        let mut length = buffer.len() as u32;
        if unsafe { QueryFullProcessImageNameW(handle, 0, buffer.as_mut_ptr(), &mut length) } == 0 {
            return Err("could not read the owned sidecar executable path".to_string());
        }
        Ok(OsString::from_wide(&buffer[..length as usize]).to_string_lossy().into_owned())
    }

    fn is_packaged_sidecar(path: &str) -> bool {
        Path::new(path)
            .file_name()
            .map(|name| name.to_string_lossy().to_ascii_lowercase().starts_with("ai-market-analyst-backend"))
            .unwrap_or(false)
    }

    pub struct ProcessHandle {
        handle: Handle,
        pid: u32,
        image_path: String,
    }

    unsafe impl Send for ProcessHandle {}
    unsafe impl Sync for ProcessHandle {}

    impl ProcessHandle {
        fn open_verified(pid: u32, expected_parent: u32, expected_image: Option<&str>) -> Result<Self, String> {
            let access = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA | PROCESS_TERMINATE | SYNCHRONIZE;
            let handle = unsafe { OpenProcess(access, 0, pid) };
            if handle.is_null() {
                return Err("could not open the owned sidecar process handle".to_string());
            }
            let path = match image_path(handle) {
                Ok(path) => path,
                Err(error) => {
                    unsafe { CloseHandle(handle); }
                    return Err(error);
                }
            };
            let image_matches = expected_image
                .map(|expected| path.eq_ignore_ascii_case(expected))
                .unwrap_or_else(|| is_packaged_sidecar(&path));
            let process = Self { handle, pid, image_path: path };
            if !process.is_alive() || parent_pid(pid) != Some(expected_parent) || !image_matches || !process.is_alive() {
                return Err("process parent or executable did not match this Tauri-owned sidecar".to_string());
            }
            Ok(process)
        }

        pub fn open_launcher(pid: u32, tauri_pid: u32) -> Result<Self, String> {
            Self::open_verified(pid, tauri_pid, None)
        }

        #[cfg(test)]
        fn open_test_child(pid: u32, expected_parent: u32) -> Result<Self, String> {
            let access = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA | PROCESS_TERMINATE | SYNCHRONIZE;
            let handle = unsafe { OpenProcess(access, 0, pid) };
            if handle.is_null() {
                return Err("could not open test child process".to_string());
            }
            let path = match image_path(handle) {
                Ok(path) => path,
                Err(error) => {
                    unsafe { CloseHandle(handle); }
                    return Err(error);
                }
            };
            let process = Self { handle, pid, image_path: path };
            if !process.is_alive() || parent_pid(pid) != Some(expected_parent) {
                return Err("test child did not have the expected parent process".to_string());
            }
            Ok(process)
        }

        fn open_worker(&self, pid: u32) -> Result<Self, String> {
            Self::open_verified(pid, self.pid, Some(&self.image_path))
        }

        pub fn pid(&self) -> u32 { self.pid }
        pub fn is_alive(&self) -> bool { unsafe { WaitForSingleObject(self.handle, 0) == WAIT_TIMEOUT } }
        fn is_in_job(&self, job: Handle) -> bool {
            let mut result = 0_i32;
            unsafe { IsProcessInJob(self.handle, job, &mut result) != 0 && result != 0 }
        }
    }

    impl Drop for ProcessHandle {
        fn drop(&mut self) { unsafe { CloseHandle(self.handle); } }
    }

    struct JobObject(Handle);

    unsafe impl Send for JobObject {}
    unsafe impl Sync for JobObject {}

    impl JobObject {
        fn new(launcher: &ProcessHandle) -> Result<Self, String> {
            let handle = unsafe { CreateJobObjectW(ptr::null(), ptr::null()) };
            if handle.is_null() {
                return Err("could not create the sidecar Job Object".to_string());
            }
            let job = Self(handle);
            let mut limits: ExtendedLimitInformation = unsafe { zeroed() };
            limits.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let configured = unsafe {
                SetInformationJobObject(
                    handle,
                    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    &limits as *const _ as *const c_void,
                    size_of::<ExtendedLimitInformation>() as u32,
                ) != 0
            };
            if !configured || unsafe { AssignProcessToJobObject(handle, launcher.handle) } == 0 {
                return Err("could not assign the launcher to its kill-on-close Job Object".to_string());
            }
            Ok(job)
        }

        fn contains(&self, process: &ProcessHandle) -> bool { process.is_in_job(self.0) }

        fn add(&self, process: &ProcessHandle) -> Result<(), String> {
            if !self.contains(process)
                && (unsafe { AssignProcessToJobObject(self.0, process.handle) } == 0 || !self.contains(process))
            {
                return Err("verified health worker could not be assigned to the sidecar Job Object".to_string());
            }
            Ok(())
        }

        fn active_process_count(&self) -> Option<u32> {
            let mut info: BasicAccountingInformation = unsafe { zeroed() };
            let mut returned = 0_u32;
            let queried = unsafe {
                QueryInformationJobObject(
                    self.0,
                    JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                    &mut info as *mut _ as *mut c_void,
                    size_of::<BasicAccountingInformation>() as u32,
                    &mut returned,
                )
            } != 0;
            queried.then_some(info.active_processes)
        }

        fn terminate_and_wait(&self, processes: &[&ProcessHandle]) -> bool {
            let _ = unsafe { TerminateJobObject(self.0, 1) };
            let deadline = Instant::now() + Duration::from_secs(8);
            while Instant::now() < deadline {
                let known_processes_exited = processes.iter().all(|process| !process.is_alive());
                if known_processes_exited && self.active_process_count() == Some(0) {
                    return true;
                }
                thread::sleep(Duration::from_millis(50));
            }
            false
        }
    }

    impl Drop for JobObject {
        fn drop(&mut self) { unsafe { CloseHandle(self.0); } }
    }

    pub struct OwnedProcessTree {
        launcher: ProcessHandle,
        job: JobObject,
        workers: Mutex<Vec<ProcessHandle>>,
        health_process_verified: AtomicBool,
    }

    unsafe impl Send for OwnedProcessTree {}
    unsafe impl Sync for OwnedProcessTree {}

    impl OwnedProcessTree {
        pub fn new(pid: u32, tauri_pid: u32) -> Result<Self, String> {
            let launcher = ProcessHandle::open_launcher(pid, tauri_pid)?;
            let job = JobObject::new(&launcher)?;
            Ok(Self {
                launcher,
                job,
                workers: Mutex::new(Vec::new()),
                health_process_verified: AtomicBool::new(false),
            })
        }

        pub fn pid(&self) -> u32 { self.launcher.pid() }
        pub fn launcher_is_alive(&self) -> bool { self.launcher.is_alive() }

        pub fn verify_health_worker(&self, pid: u32) -> Result<(), String> {
            if pid == self.launcher.pid() {
                if !self.launcher.is_alive() || !self.job.contains(&self.launcher) {
                    return Err("health process is not the live owned launcher".to_string());
                }
                self.health_process_verified.store(true, Ordering::SeqCst);
                return Ok(());
            }
            let mut workers = self.workers.lock().map_err(|_| "sidecar worker ownership lock is unavailable".to_string())?;
            if workers.iter().any(|worker| worker.pid() == pid && worker.is_alive() && self.job.contains(worker)) {
                self.health_process_verified.store(true, Ordering::SeqCst);
                return Ok(());
            }
            let worker = self.launcher.open_worker(pid)?;
            self.job.add(&worker)?;
            workers.push(worker);
            self.health_process_verified.store(true, Ordering::SeqCst);
            Ok(())
        }

        pub fn has_verified_worker(&self) -> bool {
            self.health_process_verified.load(Ordering::SeqCst)
                || self.workers.lock().map(|workers| !workers.is_empty()).unwrap_or(false)
        }

        pub fn terminate_and_wait(&self) -> bool {
            let Ok(workers) = self.workers.lock() else { return false; };
            let mut known_processes = Vec::with_capacity(workers.len() + 1);
            known_processes.push(&self.launcher);
            known_processes.extend(workers.iter());
            self.job.terminate_and_wait(&known_processes)
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::os::windows::process::CommandExt;
        use std::process::Command;

        const CREATE_NO_WINDOW: u32 = 0x0800_0000;

        #[test]
        fn job_object_termination_waits_for_a_real_windows_child() {
            let mut child = Command::new("timeout.exe")
                .args(["/T", "30", "/NOBREAK"])
                .creation_flags(CREATE_NO_WINDOW)
                .spawn()
                .expect("Windows timeout utility should be available");
            let process = match ProcessHandle::open_test_child(child.id(), std::process::id()) {
                Ok(process) => process,
                Err(error) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    panic!("retain the exact spawned child handle: {error}");
                }
            };
            let job = match JobObject::new(&process) {
                Ok(job) => job,
                Err(error) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    panic!("create and assign Windows Job Object: {error}");
                }
            };
            if let Err(error) = job.add(&process) {
                let _ = child.kill();
                let _ = child.wait();
                panic!("confirm process is in the Job Object: {error}");
            }

            let terminated = job.terminate_and_wait(&[&process]);
            let process_signaled = !process.is_alive();
            if !terminated || !process_signaled {
                let _ = child.kill();
                let _ = child.wait();
            }
            assert!(terminated, "Job Object active process count should reach zero");
            assert!(process_signaled, "retained child handle should be signaled after termination");
            let _ = child.wait();
        }
    }
}

#[cfg(not(target_os = "windows"))]
mod platform {
    pub struct OwnedProcessTree { pid: u32 }
    impl OwnedProcessTree {
        pub fn new(pid: u32, _tauri_pid: u32) -> Result<Self, String> { Ok(Self { pid }) }
        pub fn pid(&self) -> u32 { self.pid }
        pub fn launcher_is_alive(&self) -> bool { true }
        pub fn verify_health_worker(&self, _pid: u32) -> Result<(), String> { Ok(()) }
        pub fn has_verified_worker(&self) -> bool { true }
        pub fn terminate_and_wait(&self) -> bool { true }
    }
}

pub use platform::OwnedProcessTree;
