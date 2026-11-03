use std::{
    fs,
    os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt},
    path::PathBuf,
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
};
use xdp_lb::{
    generation::{MetadataState, Publication, RecoveryAction, plan_backends, reconcile},
    identity::{IdentityBook, parse_ipv4_backends},
};
use xdp_lb_common::abi::BackendAddr;
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "pw16-test-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
        Self(path)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn backend(id: u64) -> BackendAddr {
    BackendAddr::new(id, [10, 0, 0, 1], [2, 0, 0, 0, 0, 1], 1500)
}

#[test]
fn never_reuse_failed_stage_tokens_and_reject_metadata_changes() {
    let mut book = IdentityBook::default();
    assert_eq!(book.reserve(backend(1)).unwrap(), 1);
    assert_eq!(book.reserve(backend(1_u64 << 32)).unwrap(), 2);
    assert!(plan_backends(&mut book, &[]).is_err());
    assert_eq!(book.reserve(backend(9)).unwrap(), 3);
    assert_eq!(book.reserve(backend(1)).unwrap(), 1);
    let mut changed = backend(1);
    changed.dst_mac[5] = 2;
    assert!(book.reserve(changed).is_err());
    for id in 10..=4101 {
        book.reserve(backend(id)).unwrap();
    }
    assert_eq!(book.len(), 4095);
    assert!(book.reserve(backend(u64::MAX)).is_err());
    assert_eq!(book.len(), 4095);
    assert_eq!(book.reserve(backend(1)).unwrap(), 1);
    assert!(book.get(0).is_none());
}

#[test]
fn private_snapshot_roundtrip_corruption_truncation_and_burned_token() {
    let tmp = Scratch::new();
    let path = tmp.0.join("offline.book");
    let mut book = IdentityBook::initialize(&path).unwrap();
    book.reserve(backend(u64::MAX)).unwrap();
    book.save(&path).unwrap();
    assert_eq!(fs::metadata(&path).unwrap().mode() & 0o777, 0o600);
    assert_eq!(fs::metadata(&tmp.0).unwrap().mode() & 0o777, 0o700);
    book.reserve(backend(1)).unwrap();
    book.save(&path).unwrap(); // a staged candidate that will never publish
    let mut recovered = IdentityBook::load(&path).unwrap();
    assert_eq!(recovered.reserve(backend(2)).unwrap(), 3);
    assert_eq!(recovered.get(1).unwrap().logical_id, u64::MAX);
    let valid = fs::read(&path).unwrap();
    for n in [0, 7, 15, valid.len() - 1] {
        fs::write(&path, &valid[..n]).unwrap();
        assert!(IdentityBook::load(&path).is_err());
    }
    let mut bad = valid.clone();
    bad[20] ^= 1;
    fs::write(&path, &bad).unwrap();
    assert!(IdentityBook::load(&path).is_err());
    fs::write(&path, &valid).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(IdentityBook::load(&path).is_err());
    fs::remove_file(&path).unwrap();
    std::os::unix::fs::symlink(tmp.0.join("elsewhere"), &path).unwrap();
    assert!(book.save(&path).is_err());
    assert!(!tmp.0.join("elsewhere").exists());
}

#[test]
fn canonical_ipv4_and_duplicates_are_validated() {
    let vip = [10, 203, 1, 100];
    let result = parse_ipv4_backends(&["10.203.0.11".into(), "10.203.0.12".into()], vip).unwrap();
    assert_eq!(
        result[0].logical_id,
        u64::from(u32::from_be_bytes([10, 203, 0, 11]))
    );
    assert_eq!(result[0].dst_mac, [0; 6]); // offline identity only, never attach-ready
    for values in [
        vec![],
        vec!["10.203.00.11"],
        vec!["0.0.0.0"],
        vec!["224.0.0.1"],
        vec!["255.255.255.255"],
        vec!["10.203.1.100"],
        vec!["10.203.0.11", "10.203.0.11"],
        vec!["::1"],
    ] {
        assert!(
            parse_ipv4_backends(
                &values.into_iter().map(String::from).collect::<Vec<_>>(),
                vip
            )
            .is_err()
        );
    }
}

#[test]
fn kernel_publication_is_the_modeled_commit_point() {
    for disk in [
        MetadataState::Durable,
        MetadataState::Failed,
        MetadataState::NotAttempted,
    ] {
        let old = reconcile(11, 12, Ok(11), disk).unwrap();
        assert_eq!(old.publication, Publication::NotPublished);
        assert_eq!(old.action, RecoveryAction::KeepOld);
        let new = reconcile(11, 12, Ok(12), disk).unwrap();
        assert_eq!(new.publication, Publication::Published);
        assert_eq!(
            new.action,
            if disk == MetadataState::Durable {
                RecoveryAction::CandidateDurable
            } else {
                RecoveryAction::CommitCandidateMetadata
            }
        );
        for observed in [Ok(99), Err(())] {
            let uncertain = reconcile(11, 12, observed, disk).unwrap();
            assert_eq!(uncertain.publication, Publication::Indeterminate);
            assert_eq!(uncertain.action, RecoveryAction::RetainBoth);
        }
    }
    assert!(reconcile(11, 11, Ok(11), MetadataState::Durable).is_err());
    assert!(reconcile(0, 12, Ok(12), MetadataState::Durable).is_err());
}

#[test]
fn actual_cli_is_offline_and_runtime_command_fails() {
    let exe = env!("CARGO_BIN_EXE_xdp-lb");
    let tmp = Scratch::new();
    let state = tmp.0.join("offline.book");
    assert!(
        Command::new(exe)
            .args(["ctl", "init-state", "--state", state.to_str().unwrap()])
            .status()
            .unwrap()
            .success()
    );
    let output = Command::new(exe)
        .args([
            "ctl",
            "plan-backends",
            "--vip",
            "10.203.1.100",
            "--state",
            state.to_str().unwrap(),
            "10.203.0.11",
            "10.203.0.12",
        ])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.contains("OFFLINE PLAN"));
    assert!(text.contains("65537"));
    assert!(text.contains("not loaded or published"));
    assert!(text.contains("MAC/route unavailable"));
    assert_eq!(IdentityBook::load(&state).unwrap().len(), 2);
    let before = fs::read(&state).unwrap();
    for args in [
        vec!["ctl", "set-backends", "10.203.0.11"],
        vec!["ctl", "plan-backends"],
        vec!["ctl", "plan-backends", "--vip"],
        vec!["ctl", "plan-backends", "--vip", "10.203.1.100", "--unknown"],
        vec!["ctl", "init-state"],
        vec!["ctl", "init-state", "--state"],
        vec!["ctl", "init-state", "--unknown", "path"],
        vec!["bad"],
    ] {
        let output = Command::new(exe).args(&args).output().unwrap();
        assert!(!output.status.success());
        if args[1.min(args.len() - 1)] == "set-backends" {
            assert!(String::from_utf8_lossy(&output.stderr).contains("unavailable"));
        }
    }
    assert_eq!(fs::read(&state).unwrap(), before);
}

#[test]
fn stale_snapshot_cannot_erase_or_reassign_reserved_tokens() {
    let tmp = Scratch::new();
    let path = tmp.0.join("offline.book");
    let mut original = IdentityBook::initialize(&path).unwrap();
    original.reserve(backend(1)).unwrap();
    original.save(&path).unwrap();
    let mut winner = IdentityBook::load(&path).unwrap();
    winner.reserve(backend(2)).unwrap();
    winner.save(&path).unwrap();
    assert!(original.save(&path).is_err());
    original.reserve(backend(3)).unwrap();
    assert!(original.save(&path).is_err());
    let recovered = IdentityBook::load(&path).unwrap();
    assert_eq!(recovered.len(), 2);
    assert_eq!(recovered.get(2).unwrap().logical_id, 2);
}

#[test]
fn snapshot_semantics_are_checked_even_with_a_recomputed_checksum() {
    let tmp = Scratch::new();
    let path = tmp.0.join("offline.book");
    let mut book = IdentityBook::initialize(&path).unwrap();
    book.reserve(backend(0x0102030405060708)).unwrap();
    book.save(&path).unwrap();
    let valid = fs::read(&path).unwrap();
    assert_eq!(&valid[16..20], &[0, 0, 0, 1]);
    assert_eq!(&valid[20..28], &[1, 2, 3, 4, 5, 6, 7, 8]);
    for (offset, bytes) in [
        (12, [255, 255, 255, 255]),
        (16, [0, 0, 0, 0]),
        (40, [0, 0, 0, 1]),
    ] {
        let mut corrupt = valid.clone();
        corrupt[offset..offset + 4].copy_from_slice(&bytes);
        let end = corrupt.len() - 8;
        let hash = lb::maglev::hash_bytes(&corrupt[..end]);
        corrupt[end..].copy_from_slice(&hash.to_be_bytes());
        fs::write(&path, &corrupt).unwrap();
        assert!(IdentityBook::load(&path).is_err());
    }
}

#[test]
fn generation_counter_rejects_wraparound() {
    assert_eq!(xdp_lb::generation::next_generation(0).unwrap(), 1);
    assert_eq!(
        xdp_lb::generation::next_generation(u64::MAX - 1).unwrap(),
        u64::MAX
    );
    assert!(xdp_lb::generation::next_generation(u64::MAX).is_err());
}

#[test]
fn lost_snapshot_cannot_reset_tokens_through_ordinary_cli() {
    let exe = env!("CARGO_BIN_EXE_xdp-lb");
    let tmp = Scratch::new();
    let state = tmp.0.join("offline.book");
    let plan = |ip: &str| {
        Command::new(exe)
            .args([
                "ctl",
                "plan-backends",
                "--vip",
                "10.203.1.100",
                "--state",
                state.to_str().unwrap(),
                ip,
            ])
            .output()
            .unwrap()
    };
    // Ordinary planning never initializes even a brand-new location.
    assert!(!plan("10.203.0.11").status.success());
    assert!(!state.exists());
    assert!(!state.with_extension("offline-lock").exists());
    let init = || {
        Command::new(exe)
            .args(["ctl", "init-state", "--state", state.to_str().unwrap()])
            .output()
            .unwrap()
    };
    assert!(init().status.success());
    let first = plan("10.203.0.11");
    assert!(
        first.status.success(),
        "positive control: {}",
        String::from_utf8_lossy(&first.stderr)
    );
    let old = IdentityBook::load(&state).unwrap();
    assert_eq!(old.get(1).unwrap().ipv4, [10, 203, 0, 11]);
    let lock = state.with_extension("offline-lock");
    assert!(lock.exists());
    let marker = fs::read(&lock).unwrap();
    let original_bytes = fs::read(&state).unwrap();
    assert!(!init().status.success());
    assert_eq!(fs::read(&state).unwrap(), original_bytes);
    fs::remove_file(&state).unwrap();
    let second = plan("10.203.0.12");
    assert!(
        !second.status.success(),
        "lost snapshot unexpectedly accepted a new token namespace: {}",
        String::from_utf8_lossy(&second.stdout)
    );
    assert!(!String::from_utf8_lossy(&second.stdout).contains("OFFLINE PLAN"));
    assert!(!state.exists());
    assert!(IdentityBook::load(&state).is_err());
    assert!(old.save(&state).is_err());
    assert!(IdentityBook::default().save(&state).is_err());
    assert!(!init().status.success());
    assert!(!state.exists());
    assert_eq!(fs::read(&lock).unwrap(), marker);
    // Test-fixture restoration only; the product has no repair/reset command.
    fs::write(&state, &original_bytes).unwrap();
    fs::set_permissions(&state, fs::Permissions::from_mode(0o600)).unwrap();
    let resumed = plan("10.203.0.12");
    assert!(
        resumed.status.success(),
        "{}",
        String::from_utf8_lossy(&resumed.stderr)
    );
    let recovered = IdentityBook::load(&state).unwrap();
    assert_eq!(recovered.get(1), old.get(1));
    assert_eq!(recovered.get(2).unwrap().ipv4, [10, 203, 0, 12]);
    assert_eq!(recovered.len(), 2);
}

#[test]
fn library_initialization_is_create_only_and_missing_state_fails_closed() {
    let tmp = Scratch::new();
    let state = tmp.0.join("offline.book");
    let marker = state.with_extension("offline-lock");
    assert!(IdentityBook::load(&state).is_err());
    assert!(IdentityBook::default().save(&state).is_err());
    assert!(!state.exists());
    assert!(!marker.exists());
    let book = IdentityBook::initialize(&state).unwrap();
    assert!(book.is_empty());
    assert_eq!(fs::metadata(&state).unwrap().mode() & 0o777, 0o600);
    assert_eq!(fs::metadata(&marker).unwrap().mode() & 0o777, 0o600);
    let bytes = fs::read(&state).unwrap();
    assert!(IdentityBook::initialize(&state).is_err());
    assert_eq!(fs::read(&state).unwrap(), bytes);
    fs::remove_file(&state).unwrap();
    assert!(IdentityBook::initialize(&state).is_err());
    assert!(book.save(&state).is_err());
    assert!(!state.exists());

    // A snapshot without a marker cannot be loaded or overwritten by init.
    let orphan = tmp.0.join("orphan.book");
    fs::write(&orphan, &bytes).unwrap();
    fs::set_permissions(&orphan, fs::Permissions::from_mode(0o600)).unwrap();
    assert!(IdentityBook::load(&orphan).is_err());
    assert!(book.save(&orphan).is_err());
    assert!(IdentityBook::initialize(&orphan).is_err());
    assert_eq!(fs::read(&orphan).unwrap(), bytes);
    assert!(orphan.with_extension("offline-lock").exists());
}
