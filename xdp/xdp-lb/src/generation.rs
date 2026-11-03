//! Offline table construction and pure recovery decisions. No kernel operations.
use crate::identity::IdentityBook;
use std::{collections::HashSet, io};
use xdp_lb_common::abi::BackendAddr;

#[derive(Debug)]
pub struct GenerationPlan {
    table: Vec<u32>,
}
impl GenerationPlan {
    pub fn table(&self) -> &[u32] {
        &self.table
    }
    pub fn decoded(&self, book: &IdentityBook) -> io::Result<Vec<u64>> {
        self.table
            .iter()
            .map(|token| {
                book.get(*token)
                    .map(|backend| backend.logical_id)
                    .ok_or_else(|| {
                        io::Error::new(
                            io::ErrorKind::InvalidData,
                            "table token absent from identity book",
                        )
                    })
            })
            .collect()
    }
}
pub fn plan_backends(
    book: &mut IdentityBook,
    backends: &[BackendAddr],
) -> io::Result<GenerationPlan> {
    if backends.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "empty backend generation",
        ));
    }
    let mut ids = HashSet::new();
    for backend in backends {
        if !ids.insert(backend.logical_id) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "duplicate logical backend identity",
            ));
        }
        // Partial reservations remain burned if a later metadata/capacity check fails.
        book.reserve(*backend)?;
    }
    let logical_ids: Vec<_> = backends.iter().map(|backend| backend.logical_id).collect();
    let maglev = lb::maglev::Maglev::new(&logical_ids);
    let table = maglev
        .table()
        .iter()
        .map(|id| book.token(*id).expect("every Maglev identity was reserved"))
        .collect();
    Ok(GenerationPlan { table })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MetadataState {
    NotAttempted,
    Durable,
    Failed,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Publication {
    NotPublished,
    Published,
    Indeterminate,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecoveryAction {
    KeepOld,
    CommitCandidateMetadata,
    CandidateDurable,
    RetainBoth,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RecoveryDecision {
    pub publication: Publication,
    pub action: RecoveryAction,
}
/// A successful candidate observation is a publication even if disk commit failed.
/// Unknown/query failure retains both generations; it must never trigger rollback.
pub fn reconcile(
    old: u32,
    candidate: u32,
    observed: Result<u32, ()>,
    metadata: MetadataState,
) -> io::Result<RecoveryDecision> {
    if old == 0 || candidate == 0 || old == candidate {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "distinct nonzero old/candidate program IDs required",
        ));
    }
    Ok(match observed {
        Ok(id) if id == old => RecoveryDecision {
            publication: Publication::NotPublished,
            action: RecoveryAction::KeepOld,
        },
        Ok(id) if id == candidate => RecoveryDecision {
            publication: Publication::Published,
            action: if metadata == MetadataState::Durable {
                RecoveryAction::CandidateDurable
            } else {
                RecoveryAction::CommitCandidateMetadata
            },
        },
        _ => RecoveryDecision {
            publication: Publication::Indeterminate,
            action: RecoveryAction::RetainBoth,
        },
    })
}

/// Future publication numbering must stop at exhaustion; no wrapping epoch/ABA.
pub fn next_generation(current: u64) -> io::Result<u64> {
    current
        .checked_add(1)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "generation counter exhausted"))
}
