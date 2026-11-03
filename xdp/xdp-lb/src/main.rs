use std::{env, net::Ipv4Addr, path::PathBuf, process::ExitCode};
use xdp_lb::{
    generation::plan_backends,
    identity::{IdentityBook, parse_ipv4_backends},
};
fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args().skip(1).collect();
    if args.first().is_some_and(|v| v == "ctl") && args.get(1).is_some_and(|v| v == "set-backends")
    {
        return Err("ctl set-backends unavailable: Aya/kernel loading and publication are not implemented; no state changed".into());
    }
    if args.first().is_some_and(|v| v == "ctl") && args.get(1).is_some_and(|v| v == "init-state") {
        if args.len() != 4 || args[2] != "--state" {
            return Err("usage: xdp-lb ctl init-state --state <private-dir/offline.book>".into());
        }
        IdentityBook::initialize(&PathBuf::from(&args[3]))?;
        println!("OFFLINE STATE initialized; no kernel maps or attachment accessed");
        return Ok(());
    }
    if args.len() < 2 || args[0] != "ctl" || args[1] != "plan-backends" {
        return Err("usage: xdp-lb ctl plan-backends --vip <IPv4> [--state <private-dir/offline.book>] <IPv4>...".into());
    }
    let mut vip = None;
    let mut state = None;
    let mut addresses = Vec::new();
    let mut index = 2;
    while index < args.len() {
        match args[index].as_str() {
            "--vip" => {
                if vip.is_some() {
                    return Err("duplicate --vip".into());
                }
                index += 1;
                let text = args
                    .get(index)
                    .ok_or("--vip requires a canonical IPv4 address")?;
                let ip: Ipv4Addr = text.parse()?;
                if ip.to_string() != *text
                    || ip.is_unspecified()
                    || ip.is_multicast()
                    || ip.is_broadcast()
                {
                    return Err("invalid canonical service VIP".into());
                }
                vip = Some(ip.octets());
            }
            "--state" => {
                if state.is_some() {
                    return Err("duplicate --state".into());
                }
                index += 1;
                state = Some(PathBuf::from(
                    args.get(index)
                        .ok_or("--state requires an offline snapshot path")?,
                ));
            }
            option if option.starts_with('-') => {
                return Err(format!("unknown option: {option}").into());
            }
            address => addresses.push(address.to_owned()),
        }
        index += 1;
    }
    let backends = parse_ipv4_backends(&addresses, vip.ok_or("--vip is required")?)?;
    let mut book = match state.as_ref().map(|path| IdentityBook::load(path)) {
        Some(Ok(book)) => book,
        Some(Err(error)) => return Err(error.into()),
        None => IdentityBook::default(),
    };
    let plan = plan_backends(&mut book, &backends);
    // Preserve partially reserved tokens even if planning fails. No output is
    // exposed until the optional offline snapshot has been durably saved.
    if let Some(path) = &state {
        book.save(path)?;
    }
    let plan = plan?;
    println!(
        "OFFLINE PLAN: {} entries; not loaded or published",
        plan.table().len()
    );
    println!("MAC/route unavailable: identities below are not attach-ready backend metadata");
    for backend in &backends {
        println!(
            "planned ip={} logical_id={} token={}",
            Ipv4Addr::from(backend.ipv4),
            backend.logical_id,
            book.token(backend.logical_id).unwrap()
        );
    }
    println!(
        "lifetime tokens allocated={}/4095; no kernel maps or attachment accessed",
        book.len()
    );
    Ok(())
}
fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("xdp-lb: {error}");
            ExitCode::FAILURE
        }
    }
}
