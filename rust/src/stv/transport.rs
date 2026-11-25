use http_body_util::{BodyExt, Full};
use hyper::{
    HeaderMap, Method, Request, Uri,
    body::Bytes,
    client::conn::http1,
    header::{CONNECTION, CONTENT_ENCODING, CONTENT_LENGTH, HOST, HeaderValue},
};
use hyper_util::rt::TokioIo;
use std::{
    io::{self, Write},
    net::{IpAddr, SocketAddr},
    sync::{Arc, OnceLock},
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    net::{TcpStream, lookup_host},
    sync::watch,
};

#[derive(Clone)]
pub(crate) struct Cancellation(watch::Sender<bool>);

impl Default for Cancellation {
    fn default() -> Self {
        Self(watch::channel(false).0)
    }
}

impl Cancellation {
    pub(crate) fn cancel(&self) {
        self.0.send_replace(true);
    }

    pub(crate) fn is_cancelled(&self) -> bool {
        *self.0.borrow()
    }

    pub(crate) async fn cancelled(&self) {
        let mut receiver = self.0.subscribe();
        let _ = receiver.wait_for(|flag| *flag).await;
    }
}

#[derive(Debug)]
pub(crate) struct WireError {
    pub(crate) code: &'static str,
    pub(crate) message: String,
    pub(crate) http_status: Option<u16>,
    pub(crate) details: Option<Box<serde_json::Value>>,
    pub(crate) verification: Option<(String, Arc<OnceLock<bool>>)>,
}

impl WireError {
    pub(crate) fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            http_status: None,
            details: None,
            verification: None,
        }
    }
}

pub(crate) struct WireRequest {
    pub(crate) url: String,
    pub(crate) method: Method,
    pub(crate) headers: HeaderMap,
    pub(crate) body: Vec<u8>,
    pub(crate) limit: usize,
    pub(crate) fresh_ipv4: bool,
    pub(crate) timeout: Duration,
}

pub(crate) struct WireResponse {
    pub(crate) status: u16,
    pub(crate) headers: HeaderMap,
    pub(crate) body: Vec<u8>,
}

pub(crate) type Fetcher =
    dyn Fn(WireRequest, &Cancellation) -> Result<WireResponse, WireError> + Send + Sync;

pub(crate) struct Transport {
    tls: tokio_rustls::TlsConnector,
    runtime: Option<tokio::runtime::Runtime>,
    #[cfg(test)]
    pinned_fixture: Option<(&'static str, SocketAddr)>,
}

impl Drop for Transport {
    fn drop(&mut self) {
        if let Some(runtime) = self.runtime.take() {
            runtime.shutdown_background();
        }
    }
}

impl Transport {
    pub(crate) fn new() -> Result<Self, WireError> {
        Self::with_roots(rustls::RootCertStore {
            roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
        })
    }

    fn with_roots(roots: rustls::RootCertStore) -> Result<Self, WireError> {
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let tls = rustls::ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .map_err(|_| {
                WireError::new("transport_unavailable", "Không khởi động được TLS nguồn.")
            })?
            .with_root_certificates(roots)
            .with_no_client_auth();
        Ok(Self {
            tls: Arc::new(tls).into(),
            // DNS uses spawn_blocking. Keep its capacity independent of blocked HTTP handlers.
            runtime: Some(
                tokio::runtime::Builder::new_multi_thread()
                    .worker_threads(1)
                    .max_blocking_threads(8)
                    .thread_name("source-transport")
                    .enable_all()
                    .build()
                    .map_err(|_| {
                        WireError::new("transport_unavailable", "Không khởi động được HTTP nguồn.")
                    })?,
            ),
            #[cfg(test)]
            pinned_fixture: None,
        })
    }

    pub(crate) fn fetch_blocking(
        &self,
        request: WireRequest,
        cancel: &Cancellation,
    ) -> Result<WireResponse, WireError> {
        self.runtime
            .as_ref()
            .ok_or_else(network)?
            .handle()
            .block_on(self.fetch(request, cancel))
    }

    pub(crate) async fn fetch(
        &self,
        request: WireRequest,
        cancel: &Cancellation,
    ) -> Result<WireResponse, WireError> {
        let timeout = request.timeout;
        let fresh = request.fresh_ipv4;
        tokio::select! {
            biased;
            _ = cancel.cancelled() => Err(WireError::new("request_cancelled","Đã dừng yêu cầu nguồn.")),
            result = tokio::time::timeout(timeout, self.fetch_inner(request)) => result.unwrap_or_else(|_| Err(
                WireError::new("source_timeout", if fresh {
                    "Xác minh phản hồi quá lâu; chưa gửi lại đáp án."
                } else {
                    "Nguồn phản hồi quá lâu."
                })
            )),
        }
    }

    async fn resolve(
        &self,
        host: &str,
        port: u16,
        ipv4: bool,
    ) -> Result<Vec<SocketAddr>, WireError> {
        #[cfg(test)]
        if let Some((fixture, addr)) = self.pinned_fixture
            && host == fixture
        {
            return tokio::task::spawn_blocking(move || vec![addr])
                .await
                .map_err(|_| network());
        }
        let resolved = lookup_host((host, port))
            .await
            .map_err(|_| network())?
            .collect();
        checked_addresses(resolved, ipv4)
    }

    async fn fetch_inner(&self, request: WireRequest) -> Result<WireResponse, WireError> {
        let WireRequest {
            url,
            method,
            mut headers,
            body,
            limit,
            fresh_ipv4,
            ..
        } = request;
        let (uri, host, port) = target(&url)?;
        if limit == 0 || !matches!(method, Method::GET | Method::POST | Method::HEAD) {
            return Err(invalid_url());
        }
        let addresses = self.resolve(&host, port, fresh_ipv4).await?;
        let mut socket = None;
        for address in addresses {
            if let Ok(stream) = TcpStream::connect(address).await {
                socket = Some(stream);
                break;
            }
        }
        let socket = socket.ok_or_else(network)?;
        let origin = uri.authority().ok_or_else(invalid_url)?;
        headers.insert(
            HOST,
            HeaderValue::from_str(origin.as_str()).map_err(|_| invalid_url())?,
        );
        headers.insert(CONNECTION, HeaderValue::from_static("close"));
        if method == Method::POST {
            headers.insert(CONTENT_LENGTH, HeaderValue::from(body.len()));
        }
        let path = uri.path_and_query().ok_or_else(invalid_url)?.as_str();
        let secure = uri.scheme_str() == Some("https");
        let mut request = Request::builder()
            .method(method)
            .uri(path)
            .body(Full::new(body.into()))
            .map_err(|_| invalid_url())?;
        *request.headers_mut() = headers;
        if secure {
            let identity =
                rustls::pki_types::ServerName::try_from(host).map_err(|_| invalid_url())?;
            let stream = self
                .tls
                .connect(identity, socket)
                .await
                .map_err(|_| network())?;
            exchange(stream, request, limit, fresh_ipv4).await
        } else {
            exchange(socket, request, limit, fresh_ipv4).await
        }
    }
}

fn invalid_url() -> WireError {
    WireError::new(
        "source_error",
        "Chỉ chấp nhận URL HTTP(S) công khai, không kèm tài khoản hoặc cổng riêng",
    )
}

fn network() -> WireError {
    WireError::new(
        "network_error",
        "Không kết nối được nguồn. Hãy kiểm tra mạng rồi thử lại.",
    )
}

fn target(raw: &str) -> Result<(Uri, String, u16), WireError> {
    if raw
        .bytes()
        .any(|c| c <= b' ' || matches!(c, b'\\' | b'#' | 127))
    {
        return Err(invalid_url());
    }
    let parsed = url::Url::parse(raw).map_err(|_| invalid_url())?;
    let expected_port = match parsed.scheme() {
        "http" => 80,
        "https" => 443,
        _ => return Err(invalid_url()),
    };
    let uri: Uri = raw.parse().map_err(|_| invalid_url())?;
    if !parsed.username().is_empty()
        || parsed.password().is_some()
        || uri
            .authority()
            .is_none_or(|value| value.as_str().contains('@'))
        || parsed.port().is_some_and(|port| port != expected_port)
    {
        return Err(invalid_url());
    }
    let host = match parsed.host().ok_or_else(invalid_url)? {
        url::Host::Domain(host) => {
            let host = host.trim_end_matches('.');
            if !host.contains('.')
                || [
                    ".localhost",
                    ".local",
                    ".internal",
                    ".lan",
                    ".home",
                    ".test",
                    ".invalid",
                ]
                .iter()
                .any(|suffix| host.ends_with(suffix))
            {
                return Err(invalid_url());
            }
            host.to_owned()
        }
        url::Host::Ipv4(v) if crate::http::public_ip(IpAddr::V4(v)) => v.to_string(),
        url::Host::Ipv6(v) if crate::http::public_ip(IpAddr::V6(v)) => v.to_string(),
        _ => return Err(invalid_url()),
    };
    Ok((uri, host, expected_port))
}

fn checked_addresses(resolved: Vec<SocketAddr>, ipv4: bool) -> Result<Vec<SocketAddr>, WireError> {
    if resolved
        .iter()
        .any(|address| !crate::http::public_ip(address.ip()))
    {
        return Err(invalid_url());
    }
    let mut addresses = Vec::new();
    for address in resolved {
        if (!ipv4 || address.is_ipv4()) && !addresses.contains(&address) {
            addresses.push(address);
        }
    }
    if addresses.is_empty() {
        Err(network())
    } else {
        Ok(addresses)
    }
}

struct Driver(tokio::task::AbortHandle);

impl Drop for Driver {
    fn drop(&mut self) {
        self.0.abort();
    }
}

async fn exchange<S: AsyncRead + AsyncWrite + Send + Unpin + 'static>(
    stream: S,
    request: Request<Full<Bytes>>,
    limit: usize,
    fresh: bool,
) -> Result<WireResponse, WireError> {
    let (mut sender, connection) = http1::handshake(TokioIo::new(stream))
        .await
        .map_err(|_| network())?;
    let _driver = Driver(
        tokio::spawn(async {
            let _ = connection.await;
        })
        .abort_handle(),
    );
    let response = sender.send_request(request).await.map_err(|_| network())?;
    let (parts, mut body) = response.into_parts();
    if !parts.status.is_success() {
        return Ok(WireResponse {
            status: parts.status.as_u16(),
            headers: parts.headers,
            body: Vec::new(),
        });
    }
    let encoding = parts
        .headers
        .get(CONTENT_ENCODING)
        .map(HeaderValue::to_str)
        .transpose()
        .map_err(|_| WireError::new("parse_error", "Kiểu nén xác minh chưa hỗ trợ."))?
        .unwrap_or("");
    let mut output = Decoder::new(encoding, limit)?;
    let mut wire_bytes = 0usize;
    while let Some(frame) = body.frame().await {
        if let Ok(chunk) = frame.map_err(|_| network())?.into_data() {
            if fresh {
                wire_bytes = wire_bytes.saturating_add(chunk.len());
                if wire_bytes > 8 * 1024 * 1024 {
                    return Err(WireError::new("parse_error", "Phản hồi xác minh quá lớn."));
                }
            }
            output.write_chunk(&chunk).map_err(decode_error)?;
        }
    }
    let body = output.finish().map_err(decode_error)?;
    Ok(WireResponse {
        status: parts.status.as_u16(),
        headers: parts.headers,
        body,
    })
}

fn decode_error(error: io::Error) -> WireError {
    WireError::new(
        "parse_error",
        if error.kind() == io::ErrorKind::FileTooLarge {
            "Nội dung nguồn vượt giới hạn an toàn."
        } else {
            "Không giải nén được phản hồi nguồn."
        },
    )
}

struct BoundedBody {
    bytes: Vec<u8>,
    limit: usize,
}

impl Write for BoundedBody {
    fn write(&mut self, input: &[u8]) -> io::Result<usize> {
        if input.len() > self.limit.saturating_sub(self.bytes.len()) {
            return Err(io::Error::from(io::ErrorKind::FileTooLarge));
        }
        self.bytes.extend_from_slice(input);
        Ok(input.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

enum Decoder {
    Identity(BoundedBody),
    Gzip(flate2::write::MultiGzDecoder<BoundedBody>),
    Deflate(flate2::write::ZlibDecoder<BoundedBody>),
    Brotli(Box<brotli::DecompressorWriter<BoundedBody>>),
}

impl Decoder {
    fn new(encoding: &str, limit: usize) -> Result<Self, WireError> {
        let body = BoundedBody {
            bytes: Vec::new(),
            limit,
        };
        Ok(match encoding {
            "" | "identity" => Self::Identity(body),
            "gzip" => Self::Gzip(flate2::write::MultiGzDecoder::new(body)),
            "deflate" => Self::Deflate(flate2::write::ZlibDecoder::new(body)),
            "br" => Self::Brotli(Box::new(brotli::DecompressorWriter::new(body, 16 * 1024))),
            _ => {
                return Err(WireError::new(
                    "parse_error",
                    "Kiểu nén xác minh chưa hỗ trợ.",
                ));
            }
        })
    }

    fn write_chunk(&mut self, chunk: &[u8]) -> io::Result<()> {
        match self {
            Self::Identity(decoder) => decoder.write_all(chunk),
            Self::Gzip(decoder) => decoder.write_all(chunk),
            Self::Deflate(decoder) => decoder.write_all(chunk),
            Self::Brotli(decoder) => decoder.write_all(chunk),
        }
    }

    fn finish(self) -> io::Result<Vec<u8>> {
        Ok(match self {
            Self::Identity(body) => body,
            Self::Gzip(decoder) => decoder.finish()?,
            Self::Deflate(decoder) => decoder.finish()?,
            Self::Brotli(mut decoder) => {
                decoder.close()?;
                decoder
                    .into_inner()
                    .map_err(|_| io::Error::from(io::ErrorKind::InvalidData))?
            }
        }
        .bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::library::test_common::Fixture;
    use std::{
        io::{BufRead, BufReader},
        net::{Ipv4Addr, SocketAddr},
        process::{Child, Command, Stdio},
    };

    #[test]
    fn m5_checked_addresses_reject_private_and_mixed_before_ipv4_selection() {
        let public: SocketAddr = ([8, 8, 8, 8], 443).into();
        let private: SocketAddr = (Ipv4Addr::LOCALHOST, 443).into();
        assert_eq!(
            checked_addresses(vec![public, private], true)
                .unwrap_err()
                .code,
            "source_error"
        );
        assert_eq!(
            checked_addresses(vec![public, public], true).unwrap(),
            vec![public]
        );
    }

    #[test]
    fn m5_compression_keeps_delivered_body_bound_across_frames() {
        let plain = b"same decoded body";
        let mut deflate =
            flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::default());
        deflate.write_all(plain).unwrap();
        let deflate = deflate.finish().unwrap();
        let mut br = brotli::CompressorWriter::new(Vec::new(), 4096, 4, 20);
        br.write_all(plain).unwrap();
        for (encoding, compressed) in [("deflate", deflate), ("br", br.into_inner())] {
            let mut decoder = Decoder::new(encoding, plain.len()).unwrap();
            for chunk in compressed.chunks(3) {
                decoder.write_chunk(chunk).unwrap();
            }
            assert_eq!(decoder.finish().unwrap(), plain);
            let mut small = Decoder::new(encoding, plain.len() - 1).unwrap();
            let result = small.write_chunk(&compressed).and_then(|()| small.finish());
            assert!(result.is_err(), "{encoding} exceeded delivered bound");
        }
    }

    const HOSTNAME: &str = "reader-stv-fixture.example.org";
    const TLS_SERVER: &str = r#"
import gzip, json, socket, ssl, sys
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(sys.argv[1], sys.argv[2])
ctx.sni_callback = lambda client, name, ctx: setattr(client, 'seen_sni', name)
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    listener.settimeout(10)
    listener.listen()
    print(listener.getsockname()[1], flush=True)
    counter = 0
    while True:
        raw, _ = listener.accept()
        counter += 1
        raw.settimeout(10)
        try:
            client = ctx.wrap_socket(raw, server_side=True)
        except ssl.SSLError:
            raw.close()
            continue
        with client, client.makefile('rb') as stream:
            request = stream.readline().decode().strip()
            headers = {}
            while line := stream.readline().decode().strip():
                key, value = line.split(':', 1)
                headers[key.lower()] = value.strip()
            body = stream.read(int(headers.get('content-length', '0'))).decode()
            if '/stall ' in request:
                print('received', flush=True)
                try:
                    observed = client.recv(1)
                except ssl.SSLError:
                    observed = b''
                print('closed' if observed == b'' else 'unexpected', flush=True)
                break
            if '/redirect ' in request:
                client.sendall(b'HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1/no\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                continue
            if '/compressed-wire-bound ' in request:
                # 129 valid empty members with maximum gzip FEXTRA fields.
                empty = bytearray(gzip.compress(b''))
                empty[3] |= 4
                member = empty[:10] + b'\xff\xff' + bytes(65535) + empty[10:]
                payload = member * 129
                head = 'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: {}\r\nConnection: close\r\n\r\n'
                try:
                    client.sendall(head.format(len(payload)).encode() + payload)
                except OSError:
                    pass
                continue
            data = gzip.compress(json.dumps(dict(request=request, headers=headers, body=body,
                sni=client.seen_sni, connection=counter, family=raw.family)).encode())
            reply = 'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nSet-Cookie: first=1\r\nSet-Cookie: second=2\r\nContent-Length: {}\r\nConnection: close\r\n\r\n'
            client.sendall(reply.format(len(data)).encode() + data)
"#;

    struct ChildGuard(Child);

    impl Drop for ChildGuard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    fn fixture_request(path: &str) -> WireRequest {
        WireRequest {
            url: format!("https://{HOSTNAME}{path}"),
            method: Method::POST,
            headers: HeaderMap::new(),
            body: b"payload=raw%2f+%20".to_vec(),
            limit: 1024 * 1024,
            fresh_ipv4: true,
            timeout: Duration::from_secs(8),
        }
    }

    #[tokio::test]
    async fn m5_native_tls_pins_ipv4_preserves_request_bytes_and_aborts_the_socket() {
        let fixture = Fixture::new("empty");
        let server_pem = fixture.root.join("tls-leaf.pem");
        let server_key = fixture.root.join("tls-key.pem");
        let mut ca = rcgen::CertificateParams::new(Vec::new()).unwrap();
        ca.distinguished_name
            .push(rcgen::DnType::CommonName, "Reader ephemeral TLS root");
        ca.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
        ca.key_usages = vec![
            rcgen::KeyUsagePurpose::KeyCertSign,
            rcgen::KeyUsagePurpose::CrlSign,
        ];
        let issuer =
            rcgen::CertifiedIssuer::self_signed(ca, rcgen::KeyPair::generate().unwrap()).unwrap();
        let key = rcgen::KeyPair::generate().unwrap();
        let mut leaf = rcgen::CertificateParams::new(vec![HOSTNAME.to_owned()]).unwrap();
        leaf.distinguished_name
            .push(rcgen::DnType::CommonName, HOSTNAME);
        leaf.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ServerAuth];
        let cert = leaf.signed_by(&key, &issuer).unwrap();
        std::fs::write(&server_pem, cert.pem()).unwrap();
        std::fs::write(&server_key, key.serialize_pem()).unwrap();
        let mut server = ChildGuard(
            Command::new("python")
                .args(["-B", "-X", "utf8", "-c", TLS_SERVER])
                .arg(&server_pem)
                .arg(&server_key)
                .stdout(Stdio::piped())
                .spawn()
                .unwrap(),
        );
        let mut stdout = BufReader::new(server.0.stdout.take().unwrap());
        let mut ready = String::new();
        stdout.read_line(&mut ready).unwrap();
        let port = ready.trim().parse::<u16>().unwrap();
        let address = (Ipv4Addr::LOCALHOST, port).into();
        let mut roots = rustls::RootCertStore::empty();
        roots.add(issuer.der().clone()).unwrap();
        let mut transport = Transport::with_roots(roots).unwrap();
        transport.pinned_fixture = Some((HOSTNAME, address));
        let transport = Arc::new(transport);
        let cancel = Cancellation::default();
        let path = "/index.php?a=%2f&x=+&empty=&x=%20";
        for connection in 1..=2 {
            let res = transport
                .fetch(fixture_request(path), &cancel)
                .await
                .unwrap();
            assert_eq!(res.status, 200);
            let observed: serde_json::Value = serde_json::from_slice(&res.body).unwrap();
            assert_eq!(observed["request"], format!("POST {path} HTTP/1.1"));
            assert_eq!(observed["headers"]["host"], HOSTNAME);
            assert_eq!(observed["headers"]["connection"], "close");
            assert_eq!(observed["sni"], HOSTNAME);
            assert_eq!(observed["body"], "payload=raw%2f+%20");
            assert_eq!(observed["connection"], connection);
            assert_eq!(observed["family"], 2);
            assert_eq!(
                res.headers
                    .get_all("set-cookie")
                    .iter()
                    .map(|c| c.to_str().unwrap())
                    .collect::<Vec<_>>(),
                ["first=1", "second=2"]
            );
        }
        let on_transport = transport.clone();
        let saturated = std::thread::spawn(move || {
            let web = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(1)
                .max_blocking_threads(1)
                .enable_all()
                .build()
                .unwrap();
            web.block_on(async move {
                tokio::task::spawn_blocking(move || {
                    on_transport.fetch_blocking(fixture_request(path), &Cancellation::default())
                })
                .await
                .unwrap()
            })
        })
        .join()
        .unwrap()
        .unwrap();
        let observed: serde_json::Value = serde_json::from_slice(&saturated.body).unwrap();
        assert_eq!(observed["connection"], 3);
        let res = transport
            .fetch(fixture_request("/redirect"), &cancel)
            .await
            .unwrap();
        assert_eq!(res.status, 302);
        assert_eq!(res.headers["location"], "https://127.0.0.1/no");
        let mut ordinary = fixture_request("/compressed-wire-bound");
        ordinary.fresh_ipv4 = false;
        assert!(
            transport
                .fetch(ordinary, &cancel)
                .await
                .unwrap()
                .body
                .is_empty()
        );
        let fresh = transport
            .fetch(fixture_request("/compressed-wire-bound"), &cancel)
            .await
            .err()
            .unwrap();
        assert_eq!(fresh.message, "Phản hồi xác minh quá lớn.");
        let mut tiny = fixture_request(path);
        tiny.limit = 8;
        assert_eq!(
            transport.fetch(tiny, &cancel).await.err().unwrap().code,
            "parse_error"
        );
        let flag = cancel.clone();
        let wait = tokio::task::spawn_blocking(move || {
            let mut line = String::new();
            stdout.read_line(&mut line).unwrap();
            assert_eq!(line.trim(), "received");
            flag.cancel();
            line.clear();
            stdout.read_line(&mut line).unwrap();
            line.trim().to_owned()
        });
        assert_eq!(
            transport
                .fetch(fixture_request("/stall"), &cancel)
                .await
                .err()
                .unwrap()
                .code,
            "request_cancelled"
        );
        assert_eq!(wait.await.unwrap(), "closed");
        assert!(server.0.wait().unwrap().success());
    }
}
