//! ONVIF（Profile S）设备发现与 RTSP 地址获取。
//!
//! 发现走 WS-Discovery：向 239.255.255.250:3702 发送 UDP 组播 Probe，
//! 设备会单播返回 XAddrs（设备服务地址）。随后按需调用 SOAP：
//! GetCapabilities → GetProfiles → GetStreamUri，返回可直接用于任务的 RTSP 地址。
//!
//! 认证使用 ONVIF 标准的 WS-Security UsernameToken（PasswordDigest），
//! 这样不需要依赖摄像头的 HTTP Digest 配置。

use std::collections::BTreeMap;
use std::io;
use std::net::UdpSocket;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Context, Result};
use base64::Engine as _;
use serde::Serialize;

const DISCOVERY_ADDR: &str = "239.255.255.250:3702";
const WSSE_NS: &str =
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd";
const WSU_NS: &str =
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd";
const PASSWORD_DIGEST: &str =
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest";
const NONCE_BASE64: &str =
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary";

#[derive(Debug, Clone, Serialize)]
pub struct DiscoveredDevice {
    pub host: String,
    pub port: u16,
    pub xaddr: String,
    pub name: String,
    pub hardware: String,
    pub location: String,
    pub uuid: String,
}

/// 局域网 WS-Discovery 探测，`timeout` 内收集所有响应并去重。
pub fn discover(timeout: Duration) -> Result<Vec<DiscoveredDevice>> {
    let socket = UdpSocket::bind("0.0.0.0:0").context("无法创建 ONVIF 发现 socket")?;
    socket
        .set_read_timeout(Some(Duration::from_millis(250)))
        .context("无法设置 socket 超时")?;
    let probe = probe_message();
    socket
        .send_to(probe.as_bytes(), DISCOVERY_ADDR)
        .context("发送 WS-Discovery 探测失败（可能被防火墙拦截）")?;

    let deadline = Instant::now() + timeout;
    let mut buf = [0u8; 16 * 1024];
    let mut found: Vec<DiscoveredDevice> = Vec::new();
    let mut seen = BTreeMap::new();

    while Instant::now() < deadline {
        match socket.recv_from(&mut buf) {
            Ok((len, _)) => {
                let text = String::from_utf8_lossy(&buf[..len]).to_string();
                if let Some(device) = parse_discovery_response(&text) {
                    if seen.insert(device.xaddr.clone(), ()).is_none() {
                        found.push(device);
                    }
                }
            }
            Err(err)
                if matches!(
                    err.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                ) =>
            {
                continue
            }
            Err(_) => break,
        }
    }
    Ok(found)
}

/// 获取设备的 RTSP 地址（可指定 profile；不指定则用第一个）。
pub async fn resolve_stream_uri(
    xaddr: &str,
    username: &str,
    password: &str,
    profile_token: Option<&str>,
) -> Result<StreamUri> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .context("初始化 HTTP 客户端失败")?;
    let user = if username.is_empty() {
        None
    } else {
        Some((username, password))
    };

    let media_url = match soap_call(&client, xaddr, &get_capabilities_body(user), user).await {
        Ok(resp) => {
            nested_tag_text(&resp, "Media", "XAddr").unwrap_or_else(|| derive_media_url(xaddr))
        }
        Err(_) => derive_media_url(xaddr),
    };

    let profiles_resp = soap_call(&client, &media_url, &get_profiles_body(user), user)
        .await
        .with_context(|| format!("调用 GetProfiles 失败（{media_url}）"))?;
    let profiles = parse_profiles(&profiles_resp);
    if profiles.is_empty() {
        return Err(anyhow!(
            "设备未返回 Media Profile，可能不支持 ONVIF Profile S"
        ));
    }
    let chosen = match profile_token {
        Some(token) if !token.is_empty() => profiles
            .iter()
            .find(|p| p.token == token)
            .cloned()
            .ok_or_else(|| anyhow!("设备上不存在 Profile {token}"))?,
        _ => profiles[0].clone(),
    };

    let uri_resp = soap_call(
        &client,
        &media_url,
        &get_stream_uri_body(&chosen.token, user),
        user,
    )
    .await
    .with_context(|| "调用 GetStreamUri 失败".to_string())?;
    let rtsp_url = tag_text(&uri_resp, "Uri").filter(|u| !u.is_empty());
    let Some(rtsp_url) = rtsp_url else {
        return Err(anyhow!(
            "设备未返回 RTSP 地址（可能未开通 Profile S 媒体服务）"
        ));
    };

    // 设备信息是锦上添花，失败不影响取流地址。
    let device = soap_call(&client, xaddr, &get_device_info_body(user), user)
        .await
        .map(|resp| DeviceInfo {
            manufacturer: tag_text(&resp, "Manufacturer").unwrap_or_default(),
            model: tag_text(&resp, "Model").unwrap_or_default(),
            firmware: tag_text(&resp, "FirmwareVersion").unwrap_or_default(),
            serial: tag_text(&resp, "SerialNumber").unwrap_or_default(),
        })
        .unwrap_or_default();

    Ok(StreamUri {
        rtsp_url,
        profile_token: chosen.token,
        profile_name: chosen.name,
        profile_count: profiles.len(),
        device,
    })
}

#[derive(Debug, Clone, Serialize)]
pub struct StreamUri {
    pub rtsp_url: String,
    pub profile_token: String,
    pub profile_name: String,
    pub profile_count: usize,
    pub device: DeviceInfo,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct DeviceInfo {
    pub manufacturer: String,
    pub model: String,
    pub firmware: String,
    pub serial: String,
}

#[derive(Debug, Clone)]
struct Profile {
    token: String,
    name: String,
}

// ── SOAP 请求体 ─────────────────────────────────────────────────────────────

fn envelope(security: &str, body: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:wsse="{wsse}" xmlns:wsu="{wsu}">
<s:Header>{security}</s:Header>
<s:Body>{body}</s:Body>
</s:Envelope>"#,
        wsse = WSSE_NS,
        wsu = WSU_NS,
    )
}

fn get_capabilities_body(user: Option<(&str, &str)>) -> String {
    envelope(
        &security_header(user),
        "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>",
    )
}

fn get_device_info_body(user: Option<(&str, &str)>) -> String {
    envelope(&security_header(user), "<tds:GetDeviceInformation/>")
}

fn get_profiles_body(user: Option<(&str, &str)>) -> String {
    envelope(&security_header(user), "<trt:GetProfiles/>")
}

fn get_stream_uri_body(token: &str, user: Option<(&str, &str)>) -> String {
    envelope(
        &security_header(user),
        &format!(
            r#"<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup><trt:ProfileToken>{token}</trt:ProfileToken></trt:GetStreamUri>"#,
            token = xml_escape(token)
        ),
    )
}

/// WS-Security UsernameToken：digest = Base64(SHA1(nonce + created + password))。
fn security_header(user: Option<(&str, &str)>) -> String {
    let Some((username, password)) = user else {
        return String::new();
    };
    let created = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string();
    let mut nonce = [0u8; 16];
    {
        use rand::RngCore;
        rand::thread_rng().fill_bytes(&mut nonce);
    }
    let mut raw = Vec::with_capacity(nonce.len() + created.len() + password.len());
    raw.extend_from_slice(&nonce);
    raw.extend_from_slice(created.as_bytes());
    raw.extend_from_slice(password.as_bytes());
    let digest = base64::engine::general_purpose::STANDARD.encode(sha1(&raw));
    let nonce_b64 = base64::engine::general_purpose::STANDARD.encode(nonce);
    format!(
        r#"<wsse:Security s:mustUnderstand="1"><wsse:UsernameToken><wsse:Username>{user}</wsse:Username><wsse:Password Type="{digest_type}">{digest}</wsse:Password><wsse:Nonce EncodingType="{nonce_type}">{nonce}</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"#,
        user = xml_escape(username),
        digest_type = PASSWORD_DIGEST,
        digest = digest,
        nonce_type = NONCE_BASE64,
        nonce = nonce_b64,
        created = created
    )
}

async fn soap_call(
    client: &reqwest::Client,
    url: &str,
    body: &str,
    user: Option<(&str, &str)>,
) -> Result<String> {
    let mut request = client
        .post(url)
        .header(
            reqwest::header::CONTENT_TYPE,
            "application/soap+xml; charset=utf-8",
        )
        .body(body.to_string());
    if let Some((username, password)) = user {
        // 部分设备只认 HTTP Digest，WS-Security 之外再带一层 Basic 不会冲突。
        request = request.basic_auth(username, Some(password));
    }
    let response = request.send().await.context("SOAP 请求发送失败")?;
    let status = response.status();
    let text = response.text().await.context("读取 SOAP 响应失败")?;
    if !status.is_success() {
        return Err(anyhow!(
            "设备返回 HTTP {status}（{}）",
            first_line(&text).unwrap_or_else(|| "无响应内容".to_string())
        ));
    }
    Ok(text)
}

// ── 发现报文 ────────────────────────────────────────────────────────────────

fn probe_message() -> String {
    let message_id = uuid::Uuid::new_v4().to_string();
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{message_id}</w:MessageID><w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To><w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>"#
    )
}

fn parse_discovery_response(xml: &str) -> Option<DiscoveredDevice> {
    let xaddrs = tag_text(xml, "XAddrs")?;
    let xaddr = xaddrs.split_whitespace().next()?.to_string();
    if !xaddr.starts_with("http") {
        return None;
    }
    let (host, port) = host_port(&xaddr)?;
    let scopes = tag_text(xml, "Scopes").unwrap_or_default();
    let uuid = tag_text(xml, "Address")
        .map(|a| a.trim_start_matches("urn:uuid:").to_string())
        .unwrap_or_default();
    Some(DiscoveredDevice {
        host,
        port,
        xaddr,
        name: scope_value(&scopes, "name"),
        hardware: scope_value(&scopes, "hardware"),
        location: scope_value(&scopes, "location"),
        uuid,
    })
}

fn scope_value(scopes: &str, key: &str) -> String {
    let needle = format!("onvif://www.onvif.org/{key}/");
    scopes
        .split_whitespace()
        .find_map(|s| s.strip_prefix(&needle))
        .map(percent_decode)
        .unwrap_or_default()
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).ok();
            if let Some(value) = hex.and_then(|h| u8::from_str_radix(h, 16).ok()) {
                out.push(value);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).to_string()
}

fn host_port(url: &str) -> Option<(String, u16)> {
    let rest = url.split("://").nth(1)?;
    let authority = rest.split('/').next()?;
    match authority.rsplit_once(':') {
        Some((host, port)) => Some((host.to_string(), port.parse().ok()?)),
        None => Some((authority.to_string(), 80)),
    }
}

fn derive_media_url(device_xaddr: &str) -> String {
    if device_xaddr.contains("device_service") {
        device_xaddr.replace("device_service", "media_service")
    } else {
        let trimmed = device_xaddr.trim_end_matches('/');
        format!("{trimmed}/onvif/media_service")
    }
}

fn parse_profiles(xml: &str) -> Vec<Profile> {
    elements(xml, "Profiles")
        .into_iter()
        .filter_map(|elem| {
            let token = elem.attr("token")?;
            Some(Profile {
                token,
                name: {
                    let name = elem.inner_text("Name");
                    if name.is_empty() {
                        String::new()
                    } else {
                        name
                    }
                },
            })
        })
        .collect()
}

// ── 极简 XML 解析（SOAP 响应结构固定，不引入额外依赖） ─────────────────────

struct XmlElement {
    attrs: Vec<(String, String)>,
    inner: String,
}

impl XmlElement {
    fn attr(&self, name: &str) -> Option<String> {
        self.attrs
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.clone())
    }

    fn inner_text(&self, local: &str) -> String {
        tag_text(&self.inner, local).unwrap_or_default()
    }
}

fn local_name(tag: &str) -> &str {
    tag.rsplit(':').next().unwrap_or(tag)
}

fn parse_attrs(raw: &str) -> Vec<(String, String)> {
    let mut attrs = Vec::new();
    let mut rest = raw;
    while let Some(pos) = rest.find('=') {
        let before = &rest[..pos];
        let name = before.split_whitespace().last().unwrap_or("").to_string();
        let after = rest[pos + 1..].trim_start();
        let (value, consumed) = if let Some(stripped) = after.strip_prefix('"') {
            match stripped.find('"') {
                Some(end) => (
                    stripped[..end].to_string(),
                    pos + 1 + (after.len() - stripped.len()) + end + 1,
                ),
                None => (String::new(), rest.len()),
            }
        } else if let Some(stripped) = after.strip_prefix('\'') {
            match stripped.find('\'') {
                Some(end) => (
                    stripped[..end].to_string(),
                    pos + 1 + (after.len() - stripped.len()) + end + 1,
                ),
                None => (String::new(), rest.len()),
            }
        } else {
            let end = after.find(char::is_whitespace).unwrap_or(after.len());
            (after[..end].to_string(), pos + 1 + end)
        };
        if !name.is_empty() {
            attrs.push((name, value));
        }
        if consumed >= rest.len() {
            break;
        }
        rest = &rest[consumed..];
    }
    attrs
}

fn elements(xml: &str, local: &str) -> Vec<XmlElement> {
    let mut out = Vec::new();
    let mut cursor = 0;
    while let Some(rel) = xml[cursor..].find('<') {
        let start = cursor + rel;
        let rest = &xml[start..];
        if rest.starts_with("</") || rest.starts_with("<!") || rest.starts_with("<?") {
            cursor = start + 1;
            continue;
        }
        let Some(end_rel) = rest.find('>') else { break };
        let raw = &rest[1..end_rel];
        let self_closing = raw.trim_end().ends_with('/');
        let name = raw.split_whitespace().next().unwrap_or("");
        if local_name(name) != local || self_closing {
            cursor = start + end_rel + 1;
            continue;
        }
        let attrs = parse_attrs(raw);
        let body_start = start + end_rel + 1;
        match find_close(xml, body_start, local) {
            Some((inner_end, after_close)) => {
                out.push(XmlElement {
                    attrs,
                    inner: xml[body_start..inner_end].to_string(),
                });
                cursor = after_close;
            }
            None => {
                out.push(XmlElement {
                    attrs,
                    inner: xml[body_start..].to_string(),
                });
                break;
            }
        }
    }
    out
}

fn find_close(xml: &str, from: usize, local: &str) -> Option<(usize, usize)> {
    let mut cursor = from;
    while let Some(rel) = xml[cursor..].find("</") {
        let start = cursor + rel;
        let rest = &xml[start + 2..];
        let end = rest.find('>')?;
        let name = rest[..end].split_whitespace().next().unwrap_or("");
        if local_name(name) == local {
            return Some((start, start + 2 + end + 1));
        }
        cursor = start + 2 + end + 1;
    }
    None
}

fn tag_text(xml: &str, local: &str) -> Option<String> {
    let mut cursor = 0;
    while let Some(rel) = xml[cursor..].find('<') {
        let start = cursor + rel;
        let rest = &xml[start..];
        if rest.starts_with("</") || rest.starts_with("<!") || rest.starts_with("<?") {
            cursor = start + 1;
            continue;
        }
        let end_rel = rest.find('>')?;
        let raw = &rest[1..end_rel];
        let name = raw.split_whitespace().next().unwrap_or("");
        let self_closing = raw.trim_end().ends_with('/');
        if local_name(name) == local {
            if self_closing {
                return Some(String::new());
            }
            let after = &rest[end_rel + 1..];
            let text_end = after.find('<').unwrap_or(after.len());
            return Some(unescape(after[..text_end].trim()));
        }
        cursor = start + end_rel + 1;
    }
    None
}

fn nested_tag_text(xml: &str, parent: &str, child: &str) -> Option<String> {
    elements(xml, parent)
        .into_iter()
        .find_map(|elem| tag_text(&elem.inner, child))
}

fn xml_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn unescape(text: &str) -> String {
    text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

fn first_line(text: &str) -> Option<String> {
    text.lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .map(|line| line.chars().take(200).collect())
}

// ── SHA-1（WS-Security PasswordDigest 需要；只此一处，避免引入新依赖） ──────

fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let bit_len = (data.len() as u64).wrapping_mul(8);
    let mut message = data.to_vec();
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bit_len.to_be_bytes());

    for chunk in message.chunks_exact(64) {
        let mut w = [0u32; 80];
        for (i, word) in chunk.chunks_exact(4).enumerate() {
            w[i] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, word) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A827999),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(*word);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }

    let mut out = [0u8; 20];
    for (i, value) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&value.to_be_bytes());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha1_known_vectors() {
        assert_eq!(
            base64::engine::general_purpose::STANDARD.encode(sha1(b"abc")),
            "qZk+NkcGgWq6PiVxeFDCbJzQ2J0="
        );
        assert_eq!(
            base64::engine::general_purpose::STANDARD.encode(sha1(b"")),
            "2jmj7l5rSw0yVb/vlWAYkK/YBwk="
        );
    }

    #[test]
    fn parses_discovery_response() {
        let xml = r#"<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing">
<s:Body><d:ProbeMatches><d:ProbeMatch><a:EndpointReference><a:Address>urn:uuid:1234</a:Address></a:EndpointReference>
<d:Types>dn:NetworkVideoTransmitter</d:Types>
<d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/HIK%20Camera onvif://www.onvif.org/hardware/DS-2CD onvif://www.onvif.org/location/Floor1</d:Scopes>
<d:XAddrs>http://192.168.1.64:80/onvif/device_service</d:XAddrs></d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"#;
        let device = parse_discovery_response(xml).expect("device");
        assert_eq!(device.host, "192.168.1.64");
        assert_eq!(device.port, 80);
        assert_eq!(device.name, "HIK Camera");
        assert_eq!(device.hardware, "DS-2CD");
        assert_eq!(device.location, "Floor1");
        assert_eq!(device.uuid, "1234");
    }

    #[test]
    fn parses_profiles_and_stream_uri() {
        let profiles = r#"<trt:GetProfilesResponse xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<trt:Profiles token="Profile_1"><tt:Name>MainStream</tt:Name></trt:Profiles>
<trt:Profiles token="Profile_2"><tt:Name>SubStream</tt:Name></trt:Profiles></trt:GetProfilesResponse>"#;
        let list = parse_profiles(profiles);
        assert_eq!(list.len(), 2);
        assert_eq!(list[0].token, "Profile_1");
        assert_eq!(list[0].name, "MainStream");

        let uri = r#"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://192.168.1.64:554/Streaming/Channels/101?transportmode=unicast&amp;profile=Profile_1</tt:Uri></trt:MediaUri></trt:GetStreamUriResponse>"#;
        assert_eq!(
            tag_text(uri, "Uri").unwrap(),
            "rtsp://192.168.1.64:554/Streaming/Channels/101?transportmode=unicast&profile=Profile_1"
        );
    }

    #[test]
    fn parses_capabilities_media_xaddr() {
        let xml = r#"<tds:GetCapabilitiesResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<tds:Capabilities><tt:Media><tt:XAddr>http://192.168.1.64/onvif/Media</tt:XAddr><tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast></tt:StreamingCapabilities></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>"#;
        assert_eq!(
            nested_tag_text(xml, "Media", "XAddr").unwrap(),
            "http://192.168.1.64/onvif/Media"
        );
        assert_eq!(
            derive_media_url("http://192.168.1.64/onvif/device_service"),
            "http://192.168.1.64/onvif/media_service"
        );
    }

    #[test]
    fn security_header_contains_digest() {
        let header = security_header(Some(("admin", "pass123")));
        assert!(header.contains("<wsse:Username>admin</wsse:Username>"));
        assert!(header.contains("#PasswordDigest"));
        assert!(header.contains("<wsu:Created>"));
        assert!(security_header(None).is_empty());
    }
}
