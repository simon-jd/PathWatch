//! Maglev permutation table v1: sorted stable u64 IDs, 65,537 entries.
//! Integer mixing is SplitMix64's finalizer with wrapping arithmetic. Byte inputs
//! (endpoint identity/flow tuples) use FNV-1a64 in their declared wire byte order.
use crate::{EndpointId, Picker};
pub const TABLE_SIZE: usize = 65_537;
pub fn hash_bytes(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0xcbf29ce484222325, |h, b| {
        (h ^ u64::from(*b)).wrapping_mul(0x100000001b3)
    })
}
pub fn mix(mut x: u64) -> u64 {
    x = (x ^ (x >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94d049bb133111eb);
    x ^ (x >> 31)
}
pub struct Maglev {
    table: Vec<EndpointId>,
}
impl Maglev {
    pub fn new(endpoints: &[EndpointId]) -> Self {
        let mut ids = endpoints.to_vec();
        ids.sort_unstable();
        ids.dedup();
        if ids.is_empty() {
            return Self { table: Vec::new() };
        }
        let n = ids.len();
        let offsets: Vec<_> = ids.iter().map(|id| mix(*id) % TABLE_SIZE as u64).collect();
        let skips: Vec<_> = ids
            .iter()
            .map(|id| (mix(*id ^ 0x9e3779b97f4a7c15) % (TABLE_SIZE as u64 - 1)) + 1)
            .collect();
        let mut next = vec![0; n];
        let mut owners = vec![None; TABLE_SIZE];
        let mut filled = 0;
        while filled < TABLE_SIZE {
            for i in 0..n {
                let mut slot = ((offsets[i] + next[i] * skips[i]) % TABLE_SIZE as u64) as usize;
                while owners[slot].is_some() {
                    next[i] += 1;
                    slot = ((offsets[i] + next[i] * skips[i]) % TABLE_SIZE as u64) as usize;
                }
                owners[slot] = Some(ids[i]);
                next[i] += 1;
                filled += 1;
                if filled == TABLE_SIZE {
                    break;
                }
            }
        }
        Self {
            table: owners.into_iter().map(Option::unwrap).collect(),
        }
    }
    /// Export host-independent endpoint IDs; encode each as big-endian u64 for
    /// transfer to a kernel map. Kernel selection uses mix(key) % TABLE_SIZE.
    pub fn table(&self) -> &[EndpointId] {
        &self.table
    }
}
impl Picker for Maglev {
    fn pick(&self, key: Option<u64>) -> Option<EndpointId> {
        if self.table.is_empty() {
            None
        } else {
            Some(self.table[(mix(key.unwrap_or(0)) % TABLE_SIZE as u64) as usize])
        }
    }
}
