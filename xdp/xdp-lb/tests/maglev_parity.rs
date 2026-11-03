use xdp_lb::{generation::plan_backends, identity::IdentityBook};
use xdp_lb_common::abi::{BACKEND_CAPACITY, BackendAddr, FiveTuple};

fn backend(id: u64, last: u8) -> BackendAddr {
    BackendAddr::new(id, [10, 203, 0, last], [2, 0, 0, 0, 0, last], 1500)
}

#[test]
fn host_planner_full_table_decodes_to_existing_maglev() {
    let inputs = [
        backend(1, 11),
        backend(1 + (1_u64 << 32), 12),
        backend(u64::MAX, 13),
    ];
    let mut book = IdentityBook::default();
    let plan = plan_backends(&mut book, &inputs).unwrap();
    assert_eq!(plan.table().len(), 65_537);
    assert_eq!(
        plan.decoded(&book).unwrap(),
        lb::maglev::Maglev::new(&[1, 1 + (1_u64 << 32), u64::MAX]).table()
    );
    let reverse = plan_backends(&mut book, &[inputs[2], inputs[1], inputs[0]]).unwrap();
    assert_eq!(plan.table(), reverse.table());
    let removed = plan_backends(&mut book, &[inputs[0], inputs[2]]).unwrap();
    assert!(!removed.table().contains(&2));
    assert_eq!(book.get(2).unwrap().logical_id, inputs[1].logical_id);
    assert_eq!(
        plan_backends(&mut book, &inputs).unwrap().table(),
        plan.table()
    );
}

#[test]
fn abi_layout_and_explicit_packet_byte_order() {
    assert_eq!(BACKEND_CAPACITY, 4096);
    assert_eq!(std::mem::size_of::<BackendAddr>(), 24);
    assert_eq!(std::mem::align_of::<BackendAddr>(), 8);
    assert_eq!(std::mem::offset_of!(BackendAddr, ipv4), 8);
    assert_eq!(std::mem::offset_of!(BackendAddr, dst_mac), 12);
    assert_eq!(std::mem::offset_of!(BackendAddr, mtu), 18);
    assert_eq!(std::mem::offset_of!(BackendAddr, reserved), 20);
    assert_eq!(backend(u64::MAX, 11).reserved, [0; 4]);
    let tuple = FiveTuple::new([1, 2, 3, 4], [5, 6, 7, 8], 0x1234, 8080);
    assert_eq!(std::mem::size_of::<FiveTuple>(), 16);
    assert_eq!(tuple.reserved, [0; 3]);
    assert_eq!(
        tuple.hash_bytes(),
        [1, 2, 3, 4, 5, 6, 7, 8, 0x12, 0x34, 0x1f, 0x90, 6]
    );
    assert_eq!(
        tuple.table_index(),
        (lb::maglev::mix(lb::maglev::hash_bytes(&tuple.hash_bytes())) % 65537) as u32
    );
}
