//! Pure desktop policies. No renderer-supplied URL/path reaches OS integration.
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Action {
    pub request_id: String,
    pub action: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Job {
    pub job_id: String,
    pub status: String,
    pub mode: String,
    pub result_count: usize,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Status {
    pub notifications: bool,
    pub job: Option<Job>,
    pub actions: Vec<Action>,
}

fn hex_id(value: &str, len: usize) -> bool {
    value.len() == len
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
impl Status {
    pub fn valid(&self) -> bool {
        self.actions.len() <= 8
            && self
                .actions
                .iter()
                .all(|a| hex_id(&a.request_id, 32) && external_url(&a.action, 1).is_some())
            && self.job.as_ref().is_none_or(|j| {
                j.result_count <= 10000
                    && j.job_id.starts_with("job_")
                    && hex_id(&j.job_id[4..], 32)
                    && ["quick", "standard", "deep", "ip", "legacy"].contains(&j.mode.as_str())
                    && [
                        "queued",
                        "preparing",
                        "probing",
                        "measuring",
                        "enriching",
                        "finalizing",
                        "cancelling",
                        "completed",
                        "cancelled",
                        "failed",
                    ]
                    .contains(&j.status.as_str())
            })
    }
    pub fn label(&self) -> String {
        match &self.job {
            Some(j) => format!("Clash SpeedBench · {} · {}", j.mode, j.status),
            None => "Clash SpeedBench · 空闲".into(),
        }
    }
}

pub fn external_url(action: &str, port: u16) -> Option<String> {
    match action {
        "browserleaks_dns" => Some("https://browserleaks.com/dns".into()),
        "dnsleaktest" => Some("https://www.dnsleaktest.com/".into()),
        "releases" => Some("https://github.com/voodookyo/clash-speedbench/releases".into()),
        // Deliberate sole HTTP exception: this instance's fixed loopback audit,
        // never a renderer-supplied host, port, query, token or external URL.
        "browser_audit" if port > 0 => Some(format!("http://127.0.0.1:{port}/#/leak")),
        _ => None,
    }
}

#[derive(Default)]
pub struct NotificationGate {
    last_terminal: Option<String>,
}
impl NotificationGate {
    pub fn observe(&mut self, job: &Option<Job>, enabled: bool) -> Option<String> {
        let j = job.as_ref()?;
        if !["completed", "cancelled", "failed"].contains(&j.status.as_str()) {
            return None;
        }
        if self.last_terminal.as_ref() == Some(&j.job_id) {
            return None;
        }
        self.last_terminal = Some(j.job_id.clone());
        if enabled {
            Some(format!(
                "{} 任务 {}；{} 个节点已返回。请在应用内查看详情。",
                j.mode, j.status, j.result_count
            ))
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn controlled_links_reject_all_generic_os_inputs() {
        for x in [
            "https://evil.example",
            "file:///key",
            "cmd /c start",
            "../private",
            "browser_audit?key=CANARY",
        ] {
            assert!(external_url(x, 8950).is_none());
        }
        assert!(external_url("browser_audit", 0).is_none());
        assert_eq!(
            external_url("browser_audit", 8951).unwrap(),
            "http://127.0.0.1:8951/#/leak"
        );
    }
    #[test]
    fn notifications_are_off_by_default_not_replayed_and_contain_only_summary() {
        let job = Some(Job {
            job_id: "job_".to_string() + &"a".repeat(32),
            status: "completed".into(),
            mode: "quick".into(),
            result_count: 3,
        });
        let mut gate = NotificationGate::default();
        assert!(gate.observe(&job, false).is_none());
        assert!(gate.observe(&job, true).is_none()); // Enabling later does not replay old completion.
        let mut enabled = NotificationGate::default();
        assert!(enabled.observe(&job, true).unwrap().contains("3 个节点"));
        assert!(enabled.observe(&job, true).is_none());
    }
    #[test]
    fn compact_status_is_bounded_and_does_not_accept_other_fields() {
        assert!(serde_json::from_str::<Status>(
            r#"{"notifications":false,"job":null,"actions":[],"key":"CANARY"}"#
        )
        .is_err());
        let invalid:Status=serde_json::from_str(r#"{"notifications":false,"job":null,"actions":[{"request_id":"bad","action":"releases"}]}"#).unwrap();
        assert!(!invalid.valid());
    }
}
