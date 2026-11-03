//! ABI v1: native little-endian scalar map values, wire-order packet byte arrays.
//! Every byte, including reserved padding, is initialized by the constructors.
pub const BACKEND_CAPACITY: u32 = 4096;
pub const TABLE_SIZE: u32 = 65_537;
pub const CONNTRACK_CAPACITY: u32 = 65_536;

#[repr(C)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BackendAddr {
    pub logical_id: u64,
    pub ipv4: [u8; 4],
    pub dst_mac: [u8; 6],
    pub mtu: u16,
    pub reserved: [u8; 4],
}
impl BackendAddr {
    pub const fn new(logical_id: u64, ipv4: [u8; 4], dst_mac: [u8; 6], mtu: u16) -> Self {
        Self {
            logical_id,
            ipv4,
            dst_mac,
            mtu,
            reserved: [0; 4],
        }
    }
}

#[repr(C)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FiveTuple {
    pub source: [u8; 4],
    pub destination: [u8; 4],
    pub source_port: [u8; 2],
    pub destination_port: [u8; 2],
    pub protocol: u8,
    pub reserved: [u8; 3],
}
impl FiveTuple {
    pub const fn new(
        source: [u8; 4],
        destination: [u8; 4],
        source_port: u16,
        destination_port: u16,
    ) -> Self {
        Self {
            source,
            destination,
            source_port: source_port.to_be_bytes(),
            destination_port: destination_port.to_be_bytes(),
            protocol: 6,
            reserved: [0; 3],
        }
    }
    pub fn hash_bytes(&self) -> [u8; 13] {
        let mut bytes = [0; 13];
        bytes[..4].copy_from_slice(&self.source);
        bytes[4..8].copy_from_slice(&self.destination);
        bytes[8..10].copy_from_slice(&self.source_port);
        bytes[10..12].copy_from_slice(&self.destination_port);
        bytes[12] = self.protocol;
        bytes
    }
    /// Fixed13-byte FNV-1a64 followed by Task12's wrapping SplitMix64 finalizer.
    pub fn table_index(&self) -> u32 {
        let mut hash = 0xcbf29ce484222325_u64;
        for byte in self.hash_bytes() {
            hash = (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3);
        }
        hash = (hash ^ (hash >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        hash = (hash ^ (hash >> 27)).wrapping_mul(0x94d049bb133111eb);
        ((hash ^ (hash >> 31)) % u64::from(TABLE_SIZE)) as u32
    }
}
