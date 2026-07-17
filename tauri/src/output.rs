//! 校验并打开 PageFerry 生成的输出文件，收紧 renderer 到本机文件系统的边界。

use std::path::{Path, PathBuf};

use tauri::{AppHandle, Manager};
use tauri_plugin_opener::OpenerExt;

const APP_DATA_DIRECTORY: &str = "PageFerry";
const OUTPUT_DIRECTORY: &str = "outputs";

/// 解析当前构建允许打开的输出根目录。
fn allowed_output_roots(app: &AppHandle) -> Result<Vec<PathBuf>, String> {
    let resolver = app.path();
    let roots = [resolver.data_dir(), resolver.local_data_dir()]
        .into_iter()
        .filter_map(Result::ok)
        .map(|base| base.join(APP_DATA_DIRECTORY).join(OUTPUT_DIRECTORY))
        .collect::<Vec<_>>();

    // `make backend` 会把开发输出定向到仓库 `.data/outputs`，该路径不能进入 release 边界。
    #[cfg(debug_assertions)]
    let roots = {
        let mut debug_roots = roots;
        debug_roots.push(debug_output_root());
        debug_roots
    };

    if roots.is_empty() {
        return Err("系统无法解析 PageFerry 数据目录。".to_owned());
    }
    Ok(roots)
}

/// 返回仅供 debug 构建使用的仓库输出目录。
#[cfg(debug_assertions)]
fn debug_output_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("tauri crate 必须位于 PageFerry 仓库内")
        .join(".data")
        .join(OUTPUT_DIRECTORY)
}

/// Canonicalize 候选路径，并确认它是允许根目录内的普通文件。
fn validate_output_path(path: &Path, roots: &[PathBuf]) -> Result<PathBuf, String> {
    let canonical_path = path
        .canonicalize()
        .map_err(|_| "输出文件不存在或路径不可解析。".to_owned())?;
    let metadata = canonical_path
        .metadata()
        .map_err(|_| "无法读取输出文件信息。".to_owned())?;

    if !metadata.is_file() {
        return Err("输出目标不是普通文件。".to_owned());
    }

    // 根目录和文件都先 canonicalize，避免 `..` 或 symlink 绕过目录边界。
    let is_allowed = roots.iter().any(|root| {
        root.canonicalize()
            .is_ok_and(|canonical_root| canonical_path.starts_with(canonical_root))
    });
    if !is_allowed {
        return Err("只能打开 PageFerry 输出目录内的文件。".to_owned());
    }

    Ok(canonical_path)
}

/// 打开一个已完成任务的输出文件，拒绝 renderer 指定的其他本机路径。
#[tauri::command]
pub(crate) fn open_output(app: AppHandle, path: String) -> Result<(), String> {
    let canonical_path = validate_output_path(Path::new(&path), &allowed_output_roots(&app)?)?;
    let utf8_path = canonical_path
        .to_str()
        .ok_or_else(|| "输出文件路径不是有效 UTF-8。".to_owned())?;

    app.opener()
        .open_path(utf8_path, None::<&str>)
        .map_err(|_| "系统无法打开输出文件。".to_owned())
}

#[cfg(test)]
mod tests {
    //! 验证输出路径无法通过目录、前缀或 symlink 绕过允许根目录。

    use std::fs;

    use tempfile::tempdir;

    use super::validate_output_path;

    /// 允许打开输出根目录内已经存在的普通文件。
    #[test]
    fn accepts_regular_file_inside_output_root() {
        let directory = tempdir().expect("应创建临时目录");
        let root = directory.path().join("outputs");
        let file = root.join("job-1").join("translated.docx");
        fs::create_dir_all(file.parent().expect("测试文件应有父目录")).expect("应创建输出目录");
        fs::write(&file, b"document").expect("应写入测试文件");

        let validated = validate_output_path(&file, &[root]).expect("根目录内文件应通过校验");

        assert_eq!(validated, file.canonicalize().expect("应解析测试文件"));
    }

    /// 拒绝名称仅与允许根目录共享字符串前缀的相邻目录。
    #[test]
    fn rejects_sibling_with_matching_string_prefix() {
        let directory = tempdir().expect("应创建临时目录");
        let root = directory.path().join("outputs");
        let sibling = directory.path().join("outputs-copy");
        let file = sibling.join("translated.docx");
        fs::create_dir_all(&root).expect("应创建输出目录");
        fs::create_dir_all(&sibling).expect("应创建相邻目录");
        fs::write(&file, b"document").expect("应写入测试文件");

        let error = validate_output_path(&file, &[root]).expect_err("相邻目录文件必须被拒绝");

        assert_eq!(error, "只能打开 PageFerry 输出目录内的文件。");
    }

    /// 即使目录位于允许根目录内，也不能把它交给系统 opener。
    #[test]
    fn rejects_directory_inside_output_root() {
        let directory = tempdir().expect("应创建临时目录");
        let root = directory.path().join("outputs");
        let nested = root.join("job-1");
        fs::create_dir_all(&nested).expect("应创建输出目录");

        let error = validate_output_path(&nested, &[root]).expect_err("目录必须被拒绝");

        assert_eq!(error, "输出目标不是普通文件。");
    }

    /// Canonicalize 后落在根目录外的 symlink 必须被拒绝。
    #[cfg(unix)]
    #[test]
    fn rejects_symlink_that_points_outside_output_root() {
        use std::os::unix::fs::symlink;

        let directory = tempdir().expect("应创建临时目录");
        let root = directory.path().join("outputs");
        let outside = directory.path().join("outside.docx");
        let link = root.join("job-1").join("translated.docx");
        fs::create_dir_all(link.parent().expect("测试链接应有父目录")).expect("应创建输出目录");
        fs::write(&outside, b"document").expect("应写入根目录外文件");
        symlink(&outside, &link).expect("应创建测试 symlink");

        let error = validate_output_path(&link, &[root]).expect_err("越界 symlink 必须被拒绝");

        assert_eq!(error, "只能打开 PageFerry 输出目录内的文件。");
    }
}
