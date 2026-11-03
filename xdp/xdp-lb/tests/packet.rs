use xdp_lb_common::{
    abi::BackendAddr,
    packet::{Adjustments, DropReason, Outcome, Service, forward},
};
const VIP: [u8; 4] = [10, 203, 1, 100];
fn service() -> Service {
    Service {
        vip: VIP,
        port: 8080,
        source_ip: [10, 203, 0, 2],
        source_mac: [2, 0, 0, 0, 0, 2],
        mtu: 1500,
    }
}
fn backend() -> BackendAddr {
    BackendAddr::new(42, [10, 203, 0, 11], [2, 0, 0, 0, 0, 11], 1500)
}
// Independent test decoder/checksummer, deliberately not the implementation helper.
fn sum(bytes: &[u8]) -> u16 {
    let mut value: u32 = bytes
        .chunks(2)
        .map(|b| {
            if b.len() == 2 {
                u32::from(u16::from_be_bytes([b[0], b[1]]))
            } else {
                u32::from(b[0]) << 8
            }
        })
        .sum();
    while value > 65535 {
        value = (value & 65535) + (value >> 16);
    }
    !(value as u16)
}
fn ip_checksum(f: &mut [u8]) {
    f[24..26].fill(0);
    let header = usize::from(f[14] & 15) * 4;
    let c = sum(&f[14..14 + header]);
    f[24..26].copy_from_slice(&c.to_be_bytes());
}
fn tcp_checksum(f: &mut [u8]) {
    f[50..52].fill(0);
    let len = u16::from_be_bytes([f[16], f[17]]) as usize - 20;
    let mut data = Vec::new();
    data.extend_from_slice(&f[26..34]);
    data.extend_from_slice(&[0, 6]);
    data.extend_from_slice(&(len as u16).to_be_bytes());
    data.extend_from_slice(&f[34..34 + len]);
    let c = sum(&data);
    f[50..52].copy_from_slice(&c.to_be_bytes());
}
fn packet(payload: usize, options: usize) -> Vec<u8> {
    let size = 40 + options + payload;
    let mut f = vec![0; 14 + size];
    f[0..6].copy_from_slice(&service().source_mac);
    f[6..12].copy_from_slice(&[2, 0, 0, 0, 0, 3]);
    f[12..14].copy_from_slice(&0x0800_u16.to_be_bytes());
    f[14] = 0x45;
    f[16..18].copy_from_slice(&(size as u16).to_be_bytes());
    f[20] = 0x40;
    f[22] = 64;
    f[23] = 6;
    f[26..30].copy_from_slice(&[10, 203, 0, 3]);
    f[30..34].copy_from_slice(&VIP);
    f[34..36].copy_from_slice(&12345_u16.to_be_bytes());
    f[36..38].copy_from_slice(&8080_u16.to_be_bytes());
    f[46] = (((20 + options) / 4) as u8) << 4;
    f[47] = 2;
    f[48..50].copy_from_slice(&32768_u16.to_be_bytes());
    f[54..54 + options].fill(1);
    f[54 + options..].fill(0x5a);
    ip_checksum(&mut f);
    tcp_checksum(&mut f);
    f
}
fn run(f: &[u8], headroom: usize, adjust: Adjustments) -> (Result<Outcome, DropReason>, Vec<u8>) {
    let mut storage = vec![0xa5; headroom + f.len() + 16];
    storage[headroom..headroom + f.len()].copy_from_slice(f);
    let out = forward(
        &mut storage,
        headroom,
        f.len(),
        &service(),
        &backend(),
        adjust,
    );
    (out, storage)
}
#[test]
fn actual_ipip_bytes_options_odd_checksum_padding_and_ttl() {
    for (payload, options, padding) in [(0, 0, 6), (3, 12, 0), (1400, 40, 0)] {
        let original = packet(payload, options);
        let mut f = original.clone();
        f.resize(f.len() + padding, 0);
        let (result, storage) = run(&f, 20, Adjustments::Ready);
        let Outcome::Transmit { start, len, tuple } = result.unwrap() else {
            panic!("not forwarded")
        };
        assert_eq!(start, 0);
        assert_eq!(len, original.len() + 20);
        let out = &storage[start..start + len];
        assert_eq!(&out[..6], &backend().dst_mac);
        assert_eq!(&out[6..12], &service().source_mac);
        assert_eq!(out[14], 0x45);
        assert_eq!(out[23], 4);
        assert_eq!(out[22], 63);
        assert_eq!(sum(&out[14..34]), 0);
        assert_eq!(&out[26..30], &service().source_ip);
        assert_eq!(&out[30..34], &backend().ipv4);
        assert_eq!(u16::from_be_bytes([out[16], out[17]]) as usize, len - 14);
        assert_eq!(&out[20..22], &[0x40, 0]);
        assert_eq!(out[42], 63);
        assert_eq!(sum(&out[34..54]), 0);
        assert_eq!(&out[54..], &original[34..]);
        assert_eq!(tuple.source_port, 12345_u16.to_be_bytes());
        assert_eq!(tuple.destination_port, 8080_u16.to_be_bytes());
        let mut inner = vec![0; 14];
        inner.extend_from_slice(&out[34..]);
        let existing = [inner[50], inner[51]];
        tcp_checksum(&mut inner);
        assert_eq!(&inner[50..52], &existing);
    }
}
#[test]
fn every_truncated_frame_is_rejected_without_writing() {
    let f = packet(3, 12);
    for end in 0..f.len() {
        let (result, storage) = run(&f[..end], 20, Adjustments::Ready);
        assert!(result.is_err(), "length {end}");
        assert_eq!(&storage[20..20 + end], &f[..end]);
    }
}
#[test]
fn malformed_and_unsupported_service_packets_drop() {
    let base = packet(3, 0);
    for (offset, value, reason) in [
        (14, 0x46, DropReason::Ipv4Options),
        (20, 0x20, DropReason::Fragment),
        (21, 1, DropReason::Fragment),
        (22, 1, DropReason::Ttl),
        (23, 17, DropReason::UnsupportedService),
        (15, 3, DropReason::UnsupportedProfile),
        (46, 0x40, DropReason::TcpHeader),
        (46, 0xf0, DropReason::TcpHeader),
    ] {
        let mut f = base.clone();
        f[offset] = value;
        ip_checksum(&mut f);
        if offset == 46 {
            tcp_checksum(&mut f);
        }
        assert_eq!(
            run(&f, 20, Adjustments::Ready).0,
            Err(reason),
            "offset {offset}"
        );
    }
    let mut f = base.clone();
    f[24] ^= 1;
    assert_eq!(
        run(&f, 20, Adjustments::Ready).0,
        Err(DropReason::Ipv4Checksum)
    );
    let mut f = base.clone();
    *f.last_mut().unwrap() ^= 1;
    assert_eq!(
        run(&f, 20, Adjustments::Ready).0,
        Err(DropReason::TcpChecksum)
    );
    for ether in [0x8100_u16, 0x88a8] {
        let mut f = base.clone();
        f[12..14].copy_from_slice(&ether.to_be_bytes());
        assert_eq!(run(&f, 20, Adjustments::Ready).0, Err(DropReason::Vlan));
    }
    let mut f = base.clone();
    f[36..38].copy_from_slice(&80_u16.to_be_bytes());
    tcp_checksum(&mut f);
    assert_eq!(
        run(&f, 20, Adjustments::Ready).0,
        Err(DropReason::UnsupportedService)
    );
    for total in [0_u16, 19, 39, 65535] {
        let mut f = base.clone();
        f[16..18].copy_from_slice(&total.to_be_bytes());
        ip_checksum(&mut f);
        assert!(run(&f, 20, Adjustments::Ready).0.is_err());
    }
}
#[test]
fn mtu_headroom_adjustment_and_l2_failures_do_not_mutate_bytes() {
    assert!(matches!(
        run(&packet(1440, 0), 20, Adjustments::Ready).0,
        Ok(Outcome::Transmit { .. })
    ));
    assert_eq!(
        run(&packet(1441, 0), 20, Adjustments::Ready).0,
        Err(DropReason::Mtu)
    );
    let f = packet(3, 0);
    for (head, adjust, reason) in [
        (19, Adjustments::Ready, DropReason::Headroom),
        (20, Adjustments::HeadFailed, DropReason::Headroom),
        (20, Adjustments::TailFailed, DropReason::TailAdjustment),
    ] {
        let (out, buf) = run(&f, head, adjust);
        assert_eq!(out, Err(reason));
        assert_eq!(&buf[head..head + f.len()], &f);
    }
    let mut buf = vec![0; 20];
    buf.extend_from_slice(&f);
    let mut bad = backend();
    bad.dst_mac = [0; 6];
    let copy = buf.clone();
    assert_eq!(
        forward(&mut buf, 20, f.len(), &service(), &bad, Adjustments::Ready),
        Err(DropReason::Backend)
    );
    assert_eq!(buf, copy);
}
#[test]
fn non_service_packets_pass_unchanged_and_ttl_two_is_valid() {
    let mut arp = packet(0, 0);
    arp[12..14].copy_from_slice(&0x0806_u16.to_be_bytes());
    let (out, b) = run(&arp, 20, Adjustments::Ready);
    assert_eq!(out, Ok(Outcome::Pass));
    assert_eq!(&b[20..20 + arp.len()], &arp);
    let mut f = packet(3, 0);
    f[33] = 99;
    ip_checksum(&mut f);
    tcp_checksum(&mut f);
    assert_eq!(run(&f, 20, Adjustments::Ready).0, Ok(Outcome::Pass));
    let mut f = packet(0, 0);
    f[22] = 2;
    ip_checksum(&mut f);
    let (out, b) = run(&f, 20, Adjustments::Ready);
    assert!(matches!(out, Ok(Outcome::Transmit { .. })));
    assert_eq!(b[22], 1);
    assert_eq!(b[42], 1);
}

#[test]
fn inspect_exposes_validated_initial_syn_without_inventing_conntrack() {
    use xdp_lb_common::packet::inspect;
    for (flags, initial_syn) in [
        (2, true),
        (0x12, false),
        (0x10, false),
        (3, false),
        (6, false),
    ] {
        let mut f = packet(1, 0);
        f[47] = flags;
        tcp_checksum(&mut f);
        let info = inspect(&f, &service()).unwrap().unwrap();
        assert_eq!(info.initial_syn, initial_syn);
        assert_eq!(info.inner_len, 41);
    }
}

#[test]
fn malformed_buffer_ranges_and_padding_are_bounded() {
    let f = packet(0, 0);
    let mut buf = f.clone();
    let before = buf.clone();
    for (start, len) in [(usize::MAX, 2), (0, usize::MAX), (f.len(), 1)] {
        assert_eq!(
            forward(
                &mut buf,
                start,
                len,
                &service(),
                &backend(),
                Adjustments::Ready
            ),
            Err(DropReason::Bounds)
        );
        assert_eq!(buf, before);
    }
    let mut f = packet(3, 0);
    f.extend_from_slice(&[0; 10]);
    assert_eq!(run(&f, 20, Adjustments::Ready).0, Err(DropReason::Padding));
    let f = packet(3, 0);
    let mut buf = vec![0; 20];
    buf.extend_from_slice(&f);
    let before = buf.clone();
    let mut small = backend();
    small.mtu = 62;
    assert_eq!(
        forward(
            &mut buf,
            20,
            f.len(),
            &service(),
            &small,
            Adjustments::Ready
        ),
        Err(DropReason::Mtu)
    );
    assert_eq!(buf, before);
}
