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
    let mut child = tokio::process::Command::new(&host.python)
        .args([
            "-I",
            "-X",
            "utf8",
            "-m",
            "anywhere_computer",
            "management-status",
            "--state-dir",
        ])
        .arg(&host.directory)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| "管理用ランタイムを起動できません。導入先を確認してください。")?;
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
    tokio::time::timeout(Duration::from_secs(15), operation)
        .await
        .map_err(|_| {
            "状態確認がタイムアウトしました。エージェントは停止していません。".to_string()
        })?
        .map_err(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;

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

fn main() {
    let host = ManagementHost::from_arguments().unwrap_or_else(|error| {
        eprintln!("{error}");
        std::process::exit(2);
    });
    tauri::Builder::default()
        .manage(host)
        .invoke_handler(tauri::generate_handler![management_snapshot])
        .run(tauri::generate_context!())
        .expect("Management window could not start");
}
