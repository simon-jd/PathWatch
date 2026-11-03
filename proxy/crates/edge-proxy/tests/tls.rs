mod common;
use common::*;
use hyper_util::rt::{TokioExecutor, TokioIo};
use rustls::{ClientConfig, RootCertStore, pki_types::ServerName};
use std::sync::Arc;
use tokio_rustls::TlsConnector;
#[tokio::test]
async fn tls_termination_sni() {
    let upstream = Upstream::start().await;
    let mut cfg = config(upstream.addr);
    let dir = tempfile::tempdir().unwrap();
    let mut roots = RootCertStore::empty();
    let mut expected = Vec::new();
    let mut certs = Vec::new();
    for name in ["one.test", "two.test"] {
        let cert = rcgen::generate_simple_self_signed(vec![name.into()]).unwrap();
        roots.add(cert.cert.der().clone()).unwrap();
        expected.push(cert.cert.der().clone());
        let cp = dir.path().join(format!("{name}.pem"));
        let kp = dir.path().join(format!("{name}.key"));
        std::fs::write(&cp, cert.cert.pem()).unwrap();
        std::fs::write(&kp, cert.signing_key.serialize_pem()).unwrap();
        certs.push(config::Certificate {
            sni: name.into(),
            cert: cp,
            key: kp,
        });
    }
    cfg.listeners[0].tls = Some(config::Tls { certs });
    let proxy = Proxy::start(cfg).await;
    for (i, name) in ["one.test", "two.test"].iter().enumerate() {
        let mut cc = ClientConfig::builder()
            .with_root_certificates(roots.clone())
            .with_no_client_auth();
        let protocol = if i == 0 {
            b"h2".as_slice()
        } else {
            b"http/1.1".as_slice()
        };
        cc.alpn_protocols = vec![protocol.to_vec()];
        let tls = TlsConnector::from(Arc::new(cc))
            .connect(
                ServerName::try_from(name.to_string()).unwrap(),
                tokio::net::TcpStream::connect(proxy.addr).await.unwrap(),
            )
            .await
            .expect("trusted real SNI TLS handshake");
        assert_eq!(tls.get_ref().1.peer_certificates().unwrap()[0], expected[i]);
        assert_eq!(tls.get_ref().1.alpn_protocol(), Some(protocol));
        if i == 0 {
            let (mut tx, c) =
                hyper::client::conn::http2::handshake(TokioExecutor::new(), TokioIo::new(tls))
                    .await
                    .unwrap();
            let driver = tokio::spawn(async move {
                let _ = c.await;
            });
            check(
                tx.send_request(request("https://test.local/tls"))
                    .await
                    .unwrap(),
            )
            .await;
            drop(tx);
            driver.await.unwrap();
        } else {
            let (mut tx, c) = hyper::client::conn::http1::handshake(TokioIo::new(tls))
                .await
                .unwrap();
            let driver = tokio::spawn(async move {
                let _ = c.await;
            });
            check(tx.send_request(request("/tls")).await.unwrap()).await;
            drop(tx);
            driver.await.unwrap();
        }
    }
    let unknown = ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    let rejected = TlsConnector::from(Arc::new(unknown))
        .connect(
            ServerName::try_from("unconfigured.test".to_string()).unwrap(),
            tokio::net::TcpStream::connect(proxy.addr).await.unwrap(),
        )
        .await;
    assert!(
        rejected.is_err(),
        "unknown SNI must not fall back to another certificate"
    );
    proxy.close().await;
    upstream.close().await;
    dir.close().unwrap();
}
