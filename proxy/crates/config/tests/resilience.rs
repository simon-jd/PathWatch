fn text(extra: &str) -> String {
    format!(
        r#"
listeners = [{{ addr="127.0.0.1:8000" }}]
management = {{ addr="127.0.0.1:8001" }}
routes = [{{ host="test.local",path_prefix="/",cluster="echo" }}]
[[clusters]]
name="echo"
endpoints=["http://127.0.0.1:9000","http://127.0.0.1:9001"]
{extra}
"#
    )
}
#[test]
fn circuit_limits_and_default_health_are_validated() {
    let c = config::Config::parse(&text(
        "circuit={max_requests=20,max_pending=0,request_timeout_ms=20000}\nhealth={}",
    ));
    assert!(c.is_ok(), "{c:?}");
    for bad in [
        "circuit={max_requests=0}",
        "circuit={max_pending=65537}",
        "circuit={request_timeout_ms=0}",
        "health={interval_ms=0}",
    ] {
        assert!(config::Config::parse(&text(bad)).is_err());
    }
}
#[test]
fn duplicate_endpoint_and_management_listener_are_rejected() {
    assert!(config::Config::parse(&text("").replace("9001", "9000")).is_err());
    assert!(config::Config::parse(&text("").replace("8001", "8000")).is_err());
}
