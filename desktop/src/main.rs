#![cfg_attr(all(target_os = "windows", not(test)), windows_subsystem = "windows")]

use std::{path::PathBuf, process::Stdio, time::Duration};
use tokio::io::AsyncReadExt;

const SNAPSHOT_LIMIT: usize = 262_144;

fn decode_snapshot(bytes: Vec<u8>) -> Result<String, &'static str> {
    if bytes.len() > SNAPSHOT_LIMIT {
        return Err("状態情報が表示上限を超えています。");
    }
    let value: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|_| "状態の形式が不正です。")?;
    if value.get("schema_version").and_then(|v| v.as_u64()) != Some(1) {
        return Err("管理画面とランタイムの版が対応していません。");
    }
    String::from_utf8(bytes).map_err(|_| "状態の文字コードが不正です。")
}

// Development host selection is native-process input, never a WebView argument.
struct ManagementHost {
    python: PathBuf,
    directory: PathBuf,
}

impl ManagementHost {
    fn from_arguments() -> Result<Self, &'static str> {
        let arguments: Vec<_> = std::env::args_os().skip(1).collect();
        if arguments.len() != 4 || arguments[0] != "--python" || arguments[2] != "--state-dir" {
            return Err("Preview requires --python ABSOLUTE_PATH --state-dir ABSOLUTE_PATH");
        }
        let python = PathBuf::from(&arguments[1]);
        let directory = PathBuf::from(&arguments[3]);
        if !python.is_absolute() || !python.is_file() || !directory.is_absolute() {
            return Err("Select an existing Python executable and an absolute state directory");
        }
        Ok(Self { python, directory })
    }
}

#[tauri::command]
async fn management_snapshot(host: tauri::State<'_, ManagementHost>) -> Result<String, String> {
    run_management(&host, "management-status", 15).await
}

#[tauri::command]
async fn management_start(host: tauri::State<'_, ManagementHost>) -> Result<String, String> {
    run_management(&host, "management-start", 60).await
}

#[tauri::command]
async fn management_startup_status(
    host: tauri::State<'_, ManagementHost>,
) -> Result<String, String> {
    run_management(&host, "management-startup-status", 60).await
}

#[tauri::command]
async fn management_startup_enable(
    host: tauri::State<'_, ManagementHost>,
) -> Result<String, String> {
    run_management(&host, "management-startup-enable", 180).await
}

#[tauri::command]
async fn management_startup_disable(
    host: tauri::State<'_, ManagementHost>,
) -> Result<String, String> {
    run_management(&host, "management-startup-disable", 180).await
}

// Only fixed native commands call this helper; JavaScript cannot select CLI arguments.
async fn run_management(
    host: &ManagementHost,
    command: &'static str,
    seconds: u64,
) -> Result<String, String> {
    let mut process = tokio::process::Command::new(&host.python);
    // Management reads use pipes, never an interactive console. Preserve the
    // reader's ownership and timeout handling while preventing focus-stealing windows.
    #[cfg(target_os = "windows")]
    process.creation_flags(0x08000000); // CREATE_NO_WINDOW
    let mut child = process
        .args([
            "-I",
            "-X",
            "utf8",
            "-m",
            "anywhere_computer",
            command,
            "--state-dir",
        ])
        .arg(&host.directory)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| "管理用ランタイムを起動できません。導入先を確認してください。")?;
    collect_snapshot(&mut child, Duration::from_secs(seconds)).await
}

async fn collect_snapshot(
    child: &mut tokio::process::Child,
    timeout: Duration,
) -> Result<String, String> {
    let stdout = child.stdout.take().ok_or("状態の読取を開始できません。")?;
    let operation = async {
        let mut bytes = Vec::new();
        stdout
            .take((SNAPSHOT_LIMIT + 1) as u64)
            .read_to_end(&mut bytes)
            .await
            .map_err(|_| "状態を読み取れません。")?;
        if bytes.len() > SNAPSHOT_LIMIT {
            return Err("状態情報が表示上限を超えています。");
        }
        let status = child
            .wait()
            .await
            .map_err(|_| "状態確認が完了しませんでした。")?;
        if !status.success() {
            return Err("状態確認に失敗しました。CLIの診断を確認してください。");
        }
        decode_snapshot(bytes)
    };
    let result = tokio::time::timeout(timeout, operation)
        .await
        .unwrap_or(Err(
            "応答がタイムアウトしました。結果は未確認です。状態を更新して確認してください。",
        ));
    if result.is_err() {
        // Reap this short-lived reader explicitly; never address the engine PID.
        if child.kill().await.is_err() && child.try_wait().ok().flatten().is_none() {
            return Err("状態取得に失敗し、読取プロセスの終了も確認できませんでした。".into());
        }
    }
    result.map_err(str::to_string)
}

fn main() {
    let host = ManagementHost::from_arguments().unwrap_or_else(|error| {
        eprintln!("{error}");
        std::process::exit(2);
    });
    tauri::Builder::default()
        .manage(host)
        .invoke_handler(tauri::generate_handler![
            management_snapshot,
            management_start,
            management_startup_status,
            management_startup_enable,
            management_startup_disable
        ])
        .run(tauri::generate_context!())
        .expect("Management window could not start");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[ignore = "child-process fixture, invoked explicitly by the cleanup test"]
    fn reader_fixture() {
        use std::io::Write;
        if std::env::var("ANYWHERE_READER_FIXTURE").as_deref() == Ok("oversized") {
            std::io::stdout()
                .write_all(&vec![b'x'; SNAPSHOT_LIMIT + 1])
                .unwrap();
            std::io::stdout().flush().unwrap();
        }
        std::thread::sleep(Duration::from_secs(60));
    }

    #[test]
    fn reaps_timed_out_and_oversized_readers() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            for mode in ["timeout", "oversized"] {
                let mut child = tokio::process::Command::new(std::env::current_exe().unwrap())
                    .args([
                        "--exact",
                        "tests::reader_fixture",
                        "--ignored",
                        "--nocapture",
                    ])
                    .env("ANYWHERE_READER_FIXTURE", mode)
                    .stdin(Stdio::null())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::null())
                    .kill_on_drop(true)
                    .spawn()
                    .unwrap();
                let result = collect_snapshot(&mut child, Duration::from_secs(2)).await;
                let error = result.unwrap_err();
                assert!(
                    error.contains(if mode == "timeout" {
                        "タイムアウト"
                    } else {
                        "表示上限"
                    }),
                    "{error}"
                );
                assert!(child.try_wait().unwrap().is_some());
            }
        });
    }

    #[test]
    fn rejects_incompatible_or_corrupt_runtime_output() {
        for bytes in [
            b"not json".to_vec(),
            b"null".to_vec(),
            b"{\"schema_version\":2}".to_vec(),
            b"{\"schema_version\":\"1\"}".to_vec(),
            vec![0xff],
            vec![b' '; SNAPSHOT_LIMIT + 1],
        ] {
            assert!(decode_snapshot(bytes).is_err());
        }
    }

    #[test]
    fn preserves_unicode_runtime_snapshot() {
        let snapshot = "{\"schema_version\":1,\"name\":\"検証🚀\"}";
        assert_eq!(
            decode_snapshot(snapshot.as_bytes().to_vec()),
            Ok(snapshot.into())
        );
    }
}
