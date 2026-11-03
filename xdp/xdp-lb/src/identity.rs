//! Offline-only identity book. Tokens are never deleted, recycled or reassigned.
//! The checksum detects accidental corruption; it provides no authentication.
use std::{
    collections::HashSet,
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    net::Ipv4Addr,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
    sync::atomic::{AtomicU64, Ordering},
};
use xdp_lb_common::abi::{BACKEND_CAPACITY, BackendAddr};
const MAGIC: &[u8; 8] = b"PWIDB001";
const MAX_BYTES: usize = 24 + (BACKEND_CAPACITY as usize - 1) * 28;
const O_NOFOLLOW: i32 = 0x20000; // Linux open(2), the supported host platform.
static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);
fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[derive(Debug, Default, Clone)]
pub struct IdentityBook {
    entries: Vec<BackendAddr>,
}
impl IdentityBook {
    pub fn len(&self) -> usize {
        self.entries.len()
    }
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
    pub fn get(&self, token: u32) -> Option<&BackendAddr> {
        token
            .checked_sub(1)
            .and_then(|index| self.entries.get(index as usize))
    }
    pub fn token(&self, id: u64) -> Option<u32> {
        self.entries
            .iter()
            .position(|entry| entry.logical_id == id)
            .map(|index| index as u32 + 1)
    }
    pub fn reserve(&mut self, backend: BackendAddr) -> io::Result<u32> {
        if backend.reserved != [0; 4] {
            return Err(invalid("nonzero ABI reserved bytes"));
        }
        if let Some(token) = self.token(backend.logical_id) {
            if self.get(token) != Some(&backend) {
                return Err(invalid("immutable backend metadata changed"));
            }
            return Ok(token);
        }
        if self.entries.len() >= BACKEND_CAPACITY as usize - 1 {
            return Err(invalid(
                "lifetime token capacity4095 exhausted; reset is never automatic",
            ));
        }
        self.entries.push(backend);
        Ok(self.entries.len() as u32)
    }
    fn encode(&self) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(24 + self.entries.len() * 28);
        bytes.extend_from_slice(MAGIC);
        bytes.extend_from_slice(&(self.entries.len() as u32).to_be_bytes());
        bytes.extend_from_slice(&(self.entries.len() as u32 + 1).to_be_bytes());
        for (index, backend) in self.entries.iter().enumerate() {
            bytes.extend_from_slice(&(index as u32 + 1).to_be_bytes());
            bytes.extend_from_slice(&backend.logical_id.to_be_bytes());
            bytes.extend_from_slice(&backend.ipv4);
            bytes.extend_from_slice(&backend.dst_mac);
            bytes.extend_from_slice(&backend.mtu.to_be_bytes());
            bytes.extend_from_slice(&backend.reserved);
        }
        bytes.extend_from_slice(&lb::maglev::hash_bytes(&bytes).to_be_bytes());
        bytes
    }
    fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.len() < 24 || bytes.len() > MAX_BYTES || &bytes[..8] != MAGIC {
            return Err(invalid("invalid offline identity format/length/version"));
        }
        let count = u32::from_be_bytes(bytes[8..12].try_into().unwrap()) as usize;
        let next = u32::from_be_bytes(bytes[12..16].try_into().unwrap());
        if count >= BACKEND_CAPACITY as usize
            || next as usize != count + 1
            || bytes.len() != 24 + count * 28
        {
            return Err(invalid("invalid token count/next token/record length"));
        }
        let end = bytes.len() - 8;
        if lb::maglev::hash_bytes(&bytes[..end])
            != u64::from_be_bytes(bytes[end..].try_into().unwrap())
        {
            return Err(invalid("offline identity checksum mismatch"));
        }
        let mut book = Self::default();
        for (index, record) in bytes[16..end].as_chunks::<28>().0.iter().enumerate() {
            let token = u32::from_be_bytes(record[..4].try_into().unwrap());
            let backend = BackendAddr {
                logical_id: u64::from_be_bytes(record[4..12].try_into().unwrap()),
                ipv4: record[12..16].try_into().unwrap(),
                dst_mac: record[16..22].try_into().unwrap(),
                mtu: u16::from_be_bytes(record[22..24].try_into().unwrap()),
                reserved: record[24..28].try_into().unwrap(),
            };
            if token != index as u32 + 1 || book.token(backend.logical_id).is_some() {
                return Err(invalid("duplicate or nonmonotonic identity token"));
            }
            book.reserve(backend)?;
        }
        Ok(book)
    }
    /// Create an empty namespace exactly once. The lock is a permanent marker:
    /// any initialization error may leave this location unusable, never reset it.
    pub fn initialize(path: &Path) -> io::Result<Self> {
        let parent = private_parent(path)?;
        let _lock = snapshot_lock(path, true)?;
        // Persist the marker before the snapshot. An interrupted initialization
        // must not look like an unused namespace on a subsequent attempt.
        _lock.sync_all()?;
        File::open(parent)?.sync_all()?;
        let book = Self::default();
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(O_NOFOLLOW)
            .open(path)?;
        file.write_all(&book.encode())?;
        file.sync_all()?;
        File::open(parent)?.sync_all()?;
        Ok(book)
    }
    /// Reopen an initialized private snapshot, failing closed if either file is
    /// missing. Reads share the writer lock; they never initialize a namespace.
    pub fn load(path: &Path) -> io::Result<Self> {
        private_parent(path)?;
        let _lock = snapshot_lock(path, false)?;
        Self::read_snapshot(path)
    }
    fn read_snapshot(path: &Path) -> io::Result<Self> {
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(O_NOFOLLOW)
            .open(path)?;
        private_file(&file.metadata()?)?;
        if file.metadata()?.len() > MAX_BYTES as u64 {
            return Err(invalid("oversized offline identity book"));
        }
        let mut bytes = Vec::new();
        (&mut file)
            .take(MAX_BYTES as u64 + 1)
            .read_to_end(&mut bytes)?;
        Self::decode(&bytes)
    }
    /// Atomic snapshot replacement followed by directory fsync. The caller must
    /// keep the same book after failed staging; no rollback/removal API exists.
    /// A save error after rename can mean the snapshot was replaced: reload to
    /// reconcile before further allocation, never interpret it as a rollback.
    pub fn save(&self, path: &Path) -> io::Result<()> {
        let parent = private_parent(path)?;
        let _lock = snapshot_lock(path, false)?;
        let previous = Self::read_snapshot(path)?;
        if !self.entries.starts_with(&previous.entries) {
            return Err(invalid(
                "stale snapshot would erase or reassign reserved tokens",
            ));
        }
        let name = path
            .file_name()
            .ok_or_else(|| invalid("missing snapshot filename"))?
            .to_str()
            .ok_or_else(|| invalid("snapshot name must be UTF-8"))?;
        let nonce = NEXT_TEMP
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |n| n.checked_add(1))
            .map_err(|_| invalid("temporary nonce exhausted"))?;
        let temp = parent.join(format!(".{name}.{}.{nonce}.tmp", std::process::id()));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(O_NOFOLLOW)
            .open(&temp)?;
        let result = (|| {
            file.write_all(&self.encode())?;
            file.sync_all()?;
            fs::rename(&temp, path)?;
            File::open(parent)?.sync_all()
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temp);
        }
        result
    }
}
// Only initialize may create the marker. All checks and snapshot accesses are
// performed while holding this same persistent writer lock.
fn snapshot_lock(path: &Path, initialize: bool) -> io::Result<File> {
    let lock_path = path.with_extension("offline-lock");
    if lock_path == path {
        return Err(invalid("snapshot extension cannot be offline-lock"));
    }
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(initialize)
        .mode(0o600)
        .custom_flags(O_NOFOLLOW)
        .open(&lock_path)?;
    private_file(&lock.metadata()?)?;
    lock.lock()?;
    Ok(lock)
}
fn private_file(metadata: &fs::Metadata) -> io::Result<()> {
    if !metadata.is_file() || metadata.mode() & 0o7777 != 0o600 || metadata.nlink() != 1 {
        return Err(invalid(
            "snapshot must be a private0600 regular file without hard links",
        ));
    }
    Ok(())
}
fn private_parent(path: &Path) -> io::Result<&Path> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .ok_or_else(|| invalid("snapshot requires an explicit private parent directory"))?;
    let metadata = fs::symlink_metadata(parent)?;
    if !metadata.is_dir() || metadata.mode() & 0o7777 != 0o700 {
        return Err(invalid(
            "snapshot parent must be an existing private0700 directory",
        ));
    }
    Ok(parent)
}

/// No MAC discovery or routing happens here: zero MAC means an offline identity.
pub fn parse_ipv4_backends(values: &[String], vip: [u8; 4]) -> io::Result<Vec<BackendAddr>> {
    if values.is_empty() || values.len() >= BACKEND_CAPACITY as usize {
        return Err(invalid("provide1..4095 backend IPv4 addresses"));
    }
    let mut seen = HashSet::new();
    let mut backends = Vec::with_capacity(values.len());
    for value in values {
        let ip: Ipv4Addr = value
            .parse()
            .map_err(|_| invalid("invalid canonical IPv4 address"))?;
        if ip.to_string() != *value
            || ip.is_unspecified()
            || ip.is_multicast()
            || ip.is_broadcast()
            || ip.octets() == vip
            || !seen.insert(ip)
        {
            return Err(invalid(
                "duplicate, noncanonical or reserved backend IPv4 address",
            ));
        }
        backends.push(BackendAddr::new(
            u64::from(u32::from_be_bytes(ip.octets())),
            ip.octets(),
            [0; 6],
            1500,
        ));
    }
    Ok(backends)
}
