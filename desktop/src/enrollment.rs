use std::{process::Stdio, time::Duration};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout};
use tokio::sync::Mutex;

const REPLY_LIMIT: usize = 32768;

#[derive(Default)]
pub struct EnrollmentState(Mutex<Option<Worker>>);

struct Worker {
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
}

fn request(method: &str, name: Option<&str>) -> Result<Vec<u8>, String> {
    if !matches!(
        method,
        "progress" | "start" | "restart" | "poll" | "cancel" | "retry_save" | "register"
    ) {
        return Err("対応していない登録操作です。".into());
    }
    if (method == "register") != name.is_some()
        || name.is_some_and(|value| value.is_empty() || value.chars().count() > 120)
    {
        return Err("端末名を確認してください。".into());
    }
    let mut value = serde_json::json!({"method": method});
    if let Some(name) = name {
        value["name"] = serde_json::json!(name);
    }
    let mut bytes = serde_json::to_vec(&value).map_err(|_| "登録要求を作成できません。")?;
    bytes.push(b'\n');
    Ok(bytes)
}

impl Worker {
    fn spawn(host: &super::ManagementSelection) -> Result<Self, String> {
        let host = host.as_ref().map_err(|error| error.to_string())?;
        let directory = host
            .directory
            .as_ref()
            .ok_or("登録用の接続設定がありません。")?;
        let config = directory.join("enrollment-provider.json");
        if !config.is_file() {
            return Err("登録用の接続設定がありません。".into());
        }
        let mut command = tokio::process::Command::new(&host.python);
        #[cfg(target_os = "windows")]
        command.creation_flags(0x08000000);
        let mut child = command
            .args([
                "-I",
                "-X",
                "utf8",
                "-m",
                "anywhere_computer.enrollment_worker",
                "--state-dir",
            ])
            .arg(directory)
            .arg("--config")
            .arg(config)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| "登録処理を起動できません。")?;
        let input = child.stdin.take().ok_or("登録用の入力を開けません。")?;
        let output = BufReader::new(child.stdout.take().ok_or("登録用の出力を開けません。")?);
        Ok(Self {
            child,
            input,
            output,
        })
    }

    async fn exchange(&mut self, request: &[u8]) -> Result<Result<String, String>, String> {
        self.input
            .write_all(request)
            .await
            .map_err(|_| "登録要求の送信結果が不明です。")?;
        self.input
            .flush()
            .await
            .map_err(|_| "登録要求の送信結果が不明です。")?;
        let mut bytes = Vec::new();
        (&mut self.output)
            .take((REPLY_LIMIT + 1) as u64)
            .read_until(b'\n', &mut bytes)
            .await
            .map_err(|_| "登録結果を読めません。")?;
        decode_reply(&bytes)
    }
}

// A rejected command leaves the serial worker healthy. Only a broken exchange
// discards it; otherwise a pending device authorization would be lost.
fn decode_reply(bytes: &[u8]) -> Result<Result<String, String>, String> {
    if bytes.len() > REPLY_LIMIT || bytes.last() != Some(&b'\n') {
        return Err("登録処理から正しい応答を取得できません。".into());
    }
    let value: serde_json::Value =
        serde_json::from_slice(bytes).map_err(|_| "登録結果の形式が不正です。")?;
    if value.get("ok").and_then(|v| v.as_bool()) == Some(false) {
        return Ok(Err(
            "登録操作の結果を確認できません。自動では再実行しません。".into(),
        ));
    }
    if value.get("ok").and_then(|v| v.as_bool()) != Some(true)
        || value["result"]["schema_version"].as_u64() != Some(1)
    {
        return Err("登録結果の版が対応していません。".into());
    }
    Ok(Ok(value["result"].to_string()))
}

#[tauri::command]
pub async fn management_enrollment(
    host: tauri::State<'_, super::ManagementSelection>,
    state: tauri::State<'_, EnrollmentState>,
    method: String,
    name: Option<String>,
) -> Result<String, String> {
    let payload = request(&method, name.as_deref())?;
    let mut slot = state
        .0
        .try_lock()
        .map_err(|_| "登録処理の応答を待っています。")?;
    if slot.is_none() {
        *slot = Some(Worker::spawn(&host)?);
    }
    exchange_worker(&mut slot, &payload, Duration::from_secs(45)).await
}

async fn exchange_worker(
    slot: &mut Option<Worker>,
    payload: &[u8],
    deadline: Duration,
) -> Result<String, String> {
    let worker = slot.as_mut().ok_or("登録処理を開始できません。")?;
    let result = tokio::time::timeout(deadline, worker.exchange(payload))
        .await
        .unwrap_or_else(|_| Err("登録結果は未確認です。自動では再実行しません。".into()));
    if result.is_err() {
        if let Some(mut worker) = slot.take() {
            if worker.child.kill().await.is_err()
                && worker.child.try_wait().ok().flatten().is_none()
            {
                return Err("登録処理の終了を確認できません。".into());
            }
        }
    }
    result?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejection_preserves_worker_but_invalid_frames_do_not() {
        assert!(decode_reply(b"{\"ok\":false}\n").unwrap().is_err());
        let good = b"{\"ok\":true,\"result\":{\"schema_version\":1}}\n";
        assert!(decode_reply(good).unwrap().is_ok());
        assert!(decode_reply(b"{\"ok\":false}").is_err());
        assert!(decode_reply(b"not json\n").is_err());
        assert!(decode_reply(b"{\"ok\":true,\"result\":{}}\n").is_err());
        assert!(decode_reply(&vec![b' '; REPLY_LIMIT + 1]).is_err());
    }

    #[test]
    #[ignore = "protocol fixture invoked by persistent_pipe test"]
    fn enrollment_pipe_fixture() {
        use std::io::{BufRead, Write};
        println!("ENROLLMENT_FIXTURE_READY");
        std::io::stdout().flush().unwrap();
        for (index, line) in std::io::stdin().lock().lines().enumerate() {
            let _: serde_json::Value = serde_json::from_str(&line.unwrap()).unwrap();
            match index {
                0 => println!("{{\"ok\":false}}"),
                1 => println!("{{\"ok\":true,\"result\":{{\"schema_version\":1,\"requests\":2}}}}"),
                _ => {
                    std::io::stdout()
                        .write_all(&vec![b'x'; REPLY_LIMIT + 1])
                        .unwrap();
                    std::io::stdout().flush().unwrap();
                    std::thread::sleep(Duration::from_secs(60));
                }
            }
            std::io::stdout().flush().unwrap();
        }
    }

    #[test]
    fn persistent_pipe_retains_rejected_worker_and_discards_broken_reply() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            let mut child = tokio::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "enrollment::tests::enrollment_pipe_fixture",
                    "--ignored",
                    "--nocapture",
                ])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .kill_on_drop(true)
                .spawn()
                .unwrap();
            let input = child.stdin.take().unwrap();
            let mut output = BufReader::new(child.stdout.take().unwrap());
            // The Rust test harness prints a banner before the protocol fixture.
            tokio::time::timeout(Duration::from_secs(10), async {
                loop {
                    let mut line = String::new();
                    assert!(output.read_line(&mut line).await.unwrap() > 0);
                    if line.trim() == "ENROLLMENT_FIXTURE_READY" {
                        break;
                    }
                }
            })
            .await
            .unwrap();
            let pid = child.id();
            let mut slot = Some(Worker {
                child,
                input,
                output,
            });
            let payload = request("progress", None).unwrap();
            let deadline = Duration::from_secs(5);
            assert!(exchange_worker(&mut slot, &payload, deadline)
                .await
                .is_err());
            assert_eq!(slot.as_ref().unwrap().child.id(), pid);
            let response = exchange_worker(&mut slot, &payload, deadline)
                .await
                .unwrap();
            let value: serde_json::Value = serde_json::from_str(&response).unwrap();
            assert_eq!(value["requests"], 2);
            assert!(exchange_worker(&mut slot, &payload, deadline)
                .await
                .is_err());
            assert!(slot.is_none());
        });
    }

    #[test]
    fn fixed_commands_and_names_only() {
        assert!(request("terminal_start", None).is_err());
        assert!(request("progress", Some("unexpected")).is_err());
        assert!(request("register", None).is_err());
        assert!(request("register", Some(&"x".repeat(121))).is_err());
        let bytes = request("register", Some("日本語 PC 🚀")).unwrap();
        let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(value["name"], "日本語 PC 🚀");
        assert_eq!(bytes.last(), Some(&b'\n'));
    }
}
