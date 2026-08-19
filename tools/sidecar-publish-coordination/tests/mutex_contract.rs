use sidecar_publish_coordination::windows_mutex::{mutex_name_for_root, NamedMutex, WaitStatus};

#[test]
fn equivalent_root_spellings_share_one_deterministic_mutex_name() {
    let first = mutex_name_for_root(r"C:\runtime\sidecar").unwrap();
    let second = mutex_name_for_root(r"c:/runtime/sidecar/.").unwrap();
    let third = mutex_name_for_root(r"C:/runtime/other/../sidecar").unwrap();
    assert_eq!(first, second);
    assert_eq!(first, third);
    assert!(first.starts_with(r"Local\jax-sidecar-publish-v1-"));
}

#[test]
fn distinct_roots_do_not_share_mutex_name() {
    assert_ne!(
        mutex_name_for_root(r"C:\runtime\one").unwrap(),
        mutex_name_for_root(r"C:\runtime\two").unwrap()
    );
}

#[test]
fn wait_status_keeps_abandoned_distinct_from_success() {
    assert_ne!(WaitStatus::Object, WaitStatus::Abandoned);
    assert_ne!(WaitStatus::Timeout, WaitStatus::Abandoned);
    assert_ne!(WaitStatus::Failed(5), WaitStatus::Abandoned);
}

#[test]
fn non_absolute_runtime_root_is_rejected() {
    assert!(mutex_name_for_root(r"runtime\sidecar").is_err());
}

#[test]
#[cfg(windows)]
fn a_second_named_mutex_wait_reports_bounded_timeout_until_the_owner_releases() {
    let name = mutex_name_for_root(r"C:\runtime\contention").unwrap();
    let worker_name = name.clone();
    let (held_tx, held_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let worker = std::thread::spawn(move || {
        let first = NamedMutex::open(&worker_name).unwrap();
        assert_eq!(first.wait(0), WaitStatus::Object);
        held_tx.send(()).unwrap();
        release_rx.recv().unwrap();
        first.release().unwrap();
    });

    held_rx.recv().unwrap();
    let second = NamedMutex::open(&name).unwrap();
    assert_eq!(second.wait(20), WaitStatus::Timeout);
    release_tx.send(()).unwrap();
    worker.join().unwrap();
    assert_eq!(second.wait(0), WaitStatus::Object);
    second.release().unwrap();
}
