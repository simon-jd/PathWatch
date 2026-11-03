//! Bounded single-buffer Ethernet/IPv4/TCP byte processing, without allocation.
//! These functions run on host slices today. Aya verifier/helper integration is
//! still required; an Adjustments result is not proof a kernel helper ran.
use crate::abi::{BackendAddr, FiveTuple};
const ETH: usize = 14;
const IPV4: usize = 20;
const MAX_INNER: usize = 1480;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Service {
    pub vip: [u8; 4],
    pub port: u16,
    pub source_ip: [u8; 4],
    pub source_mac: [u8; 6],
    pub mtu: u16,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Adjustments {
    Ready,
    HeadFailed,
    TailFailed,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DropReason {
    Bounds,
    Vlan,
    Ipv4Header,
    Ipv4Options,
    Ipv4Length,
    Ipv4Checksum,
    Fragment,
    UnsupportedService,
    UnsupportedProfile,
    TcpHeader,
    TcpChecksum,
    Ttl,
    Mtu,
    Padding,
    Backend,
    Headroom,
    TailAdjustment,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Pass,
    Transmit {
        start: usize,
        len: usize,
        tuple: FiveTuple,
    },
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PacketInfo {
    pub inner_len: usize,
    pub tuple: FiveTuple,
    pub ttl: u8,
    pub initial_syn: bool,
}
fn word(bytes: &[u8], at: usize) -> u16 {
    u16::from_be_bytes([bytes[at], bytes[at + 1]])
}
fn sum(bytes: &[u8]) -> u32 {
    let mut total = 0_u32;
    // Caller bounds all slices to at most1480 bytes. No unbounded packet loop.
    let mut index = 0;
    while index + 1 < bytes.len() {
        total += u32::from(word(bytes, index));
        index += 2;
    }
    if index < bytes.len() {
        total += u32::from(bytes[index]) << 8;
    }
    total
}
fn checksum(total: u32) -> u16 {
    let first = (total & 0xffff) + (total >> 16);
    let folded = (first & 0xffff) + (first >> 16);
    !(folded as u16)
}
/// Validate service bytes before any conntrack lookup or insertion. None passes.
pub fn inspect(frame: &[u8], service: &Service) -> Result<Option<PacketInfo>, DropReason> {
    if frame.len() < ETH {
        return Err(DropReason::Bounds);
    }
    match word(frame, 12) {
        0x8100 | 0x88a8 => return Err(DropReason::Vlan),
        0x0800 => (),
        _ => return Ok(None),
    }
    if frame.len() < ETH + IPV4 {
        return Err(DropReason::Bounds);
    }
    let ip = &frame[ETH..];
    if ip[0] >> 4 != 4 {
        return Err(DropReason::Ipv4Header);
    }
    let ihl = usize::from(ip[0] & 15) * 4;
    if ihl < IPV4 || ip.len() < ihl {
        return Err(DropReason::Ipv4Header);
    }
    let inner_len = usize::from(word(ip, 2));
    if inner_len < ihl || inner_len > ip.len() {
        return Err(DropReason::Ipv4Length);
    }
    if checksum(sum(&ip[..ihl])) != 0 {
        return Err(DropReason::Ipv4Checksum);
    }
    if ip[16..20] != service.vip {
        return Ok(None);
    }
    if ihl != IPV4 {
        return Err(DropReason::Ipv4Options);
    }
    if word(ip, 6) & 0x3fff != 0 {
        return Err(DropReason::Fragment);
    }
    if ip[1] != 0 || word(ip, 6) & 0x8000 != 0 {
        return Err(DropReason::UnsupportedProfile);
    }
    if ip[9] != 6 {
        return Err(DropReason::UnsupportedService);
    }
    if inner_len < IPV4 + 20 {
        return Err(DropReason::TcpHeader);
    }
    if inner_len > MAX_INNER || inner_len + IPV4 > usize::from(service.mtu) {
        return Err(DropReason::Mtu);
    }
    let tcp = &ip[IPV4..inner_len];
    let tcp_header = usize::from(tcp[12] >> 4) * 4;
    if !(20..=60).contains(&tcp_header) || tcp_header > tcp.len() {
        return Err(DropReason::TcpHeader);
    }
    if word(tcp, 2) != service.port {
        return Err(DropReason::UnsupportedService);
    }
    let pseudo = sum(&ip[12..20]) + 6 + tcp.len() as u32;
    if checksum(pseudo + sum(tcp)) != 0 {
        return Err(DropReason::TcpChecksum);
    }
    if ip[8] <= 1 {
        return Err(DropReason::Ttl);
    }
    // Only minimum Ethernet-frame padding is accepted; extra trailing bytes are
    // not silently discarded as if they belonged to a second packet.
    let used = ETH + inner_len;
    if frame.len() > used && (frame.len() > 60 || frame[used..].iter().any(|byte| *byte != 0)) {
        return Err(DropReason::Padding);
    }
    Ok(Some(PacketInfo {
        inner_len,
        initial_syn: tcp[13] & 0x17 == 0x02,
        tuple: FiveTuple::new(
            ip[12..16].try_into().unwrap(),
            ip[16..20].try_into().unwrap(),
            word(tcp, 0),
            word(tcp, 2),
        ),
        ttl: ip[8],
    }))
}
fn unicast(mac: [u8; 6]) -> bool {
    mac != [0; 6] && mac[0] & 1 == 0
}
/// Apply a complete encapsulation to an existing frame with20 bytes of headroom.
/// On every error or Pass, storage is unchanged. Success returns the trimmed
/// transmit range; bytes outside that range must not be transmitted.
pub fn forward(
    storage: &mut [u8],
    start: usize,
    len: usize,
    service: &Service,
    backend: &BackendAddr,
    adjustments: Adjustments,
) -> Result<Outcome, DropReason> {
    let end = start
        .checked_add(len)
        .filter(|end| *end <= storage.len())
        .ok_or(DropReason::Bounds)?;
    let Some(parsed) = inspect(&storage[start..end], service)? else {
        return Ok(Outcome::Pass);
    };
    if !unicast(backend.dst_mac)
        || !unicast(service.source_mac)
        || backend.reserved != [0; 4]
        || backend.ipv4 == [0; 4]
        || backend.ipv4[0] >= 224
        || backend.ipv4 == service.vip
    {
        return Err(DropReason::Backend);
    }
    if parsed.inner_len + IPV4 > usize::from(backend.mtu) {
        return Err(DropReason::Mtu);
    }
    if adjustments == Adjustments::TailFailed {
        return Err(DropReason::TailAdjustment);
    }
    let new_start = start.checked_sub(IPV4).ok_or(DropReason::Headroom)?;
    if adjustments == Adjustments::HeadFailed {
        return Err(DropReason::Headroom);
    }
    let new_len = ETH + IPV4 + parsed.inner_len;
    let out = &mut storage[new_start..new_start + new_len];
    // Original inner IP is already at offset34 after the20-byte head adjustment.
    let inner = &mut out[ETH + IPV4..];
    inner[8] = parsed.ttl - 1;
    inner[10..12].fill(0);
    let inner_checksum = checksum(sum(&inner[..IPV4]));
    inner[10..12].copy_from_slice(&inner_checksum.to_be_bytes());
    out[..6].copy_from_slice(&backend.dst_mac);
    out[6..12].copy_from_slice(&service.source_mac);
    out[12..14].copy_from_slice(&0x0800_u16.to_be_bytes());
    let outer = &mut out[ETH..ETH + IPV4];
    outer.fill(0);
    outer[0] = 0x45;
    outer[2..4].copy_from_slice(&((parsed.inner_len + IPV4) as u16).to_be_bytes());
    outer[6] = 0x40;
    outer[8] = parsed.ttl - 1;
    outer[9] = 4;
    outer[12..16].copy_from_slice(&service.source_ip);
    outer[16..20].copy_from_slice(&backend.ipv4);
    let outer_checksum = checksum(sum(outer));
    outer[10..12].copy_from_slice(&outer_checksum.to_be_bytes());
    Ok(Outcome::Transmit {
        start: new_start,
        len: new_len,
        tuple: parsed.tuple,
    })
}
