import sys
import tempfile
import unittest
from pathlib import Path

FIN6_DIR = Path(__file__).resolve().parents[1]
if str(FIN6_DIR) not in sys.path:
    sys.path.insert(0, str(FIN6_DIR))

from box_upload import (
    collect_artifacts,
    discover_legacy_box_settings,
    resolve_box_config,
    resolve_effective_box_settings,
    should_auto_upload,
    write_box_templates,
)
from box_upload import BoxUploadError
from shared_utils import load_json, save_json


class BoxUploadTests(unittest.TestCase):
    def test_write_box_templates_and_resolve_effective_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = write_box_templates(tmpdir)
            config_path = Path(result["config_file"])
            tokens_path = Path(result["tokens_file"])
            env_path = Path(result["env_file"])
            self.assertTrue(config_path.exists())
            self.assertTrue(tokens_path.exists())
            self.assertTrue(env_path.exists())

            effective = resolve_effective_box_settings({"config_file": str(config_path)})
            self.assertTrue(effective.enabled)
            self.assertTrue(effective.upload_after_processing)
            self.assertFalse(effective.upload_after_recording)
            self.assertEqual(effective.artifact_mode, "summaries+videos")
            self.assertEqual(Path(effective.tokens_file), tokens_path)
            self.assertTrue(should_auto_upload({"config_file": str(config_path)}, "processing"))
            self.assertFalse(should_auto_upload({"config_file": str(config_path)}, "recording"))

    def test_collect_artifacts_filters_backgrounds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "assay_20260411_120000"
            (run_dir / "processed").mkdir(parents=True)
            (run_dir / "graphs").mkdir(parents=True)
            for file_path in [
                run_dir / "run_manifest.json",
                run_dir / "background_raw_snapshot.png",
                run_dir / "background_meta_snapshot.json",
                run_dir / "processed" / "frame_level.csv",
                run_dir / "processed" / "annotated_video.mp4",
                run_dir / "graphs" / "velocity_plot.png",
            ]:
                if file_path.suffix == ".json":
                    save_json(file_path, {"ok": True})
                else:
                    file_path.write_bytes(b"test")

            summaries = collect_artifacts(run_dir, mode="summaries", include_backgrounds=False)
            summary_names = {path.name for path in summaries}
            self.assertIn("run_manifest.json", summary_names)
            self.assertIn("frame_level.csv", summary_names)
            self.assertIn("velocity_plot.png", summary_names)
            self.assertNotIn("background_raw_snapshot.png", summary_names)
            self.assertNotIn("background_meta_snapshot.json", summary_names)
            self.assertNotIn("annotated_video.mp4", summary_names)

            with_videos = collect_artifacts(run_dir, mode="summaries+videos", include_backgrounds=False)
            video_names = {path.name for path in with_videos}
            self.assertIn("annotated_video.mp4", video_names)
            self.assertNotIn("background_raw_snapshot.png", video_names)

            with_backgrounds = collect_artifacts(run_dir, mode="summaries+videos", include_backgrounds=True)
            bg_names = {path.name for path in with_backgrounds}
            self.assertIn("background_raw_snapshot.png", bg_names)
            self.assertIn("background_meta_snapshot.json", bg_names)


    def test_collect_artifacts_core_mode_uses_latest_processing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "assay_20260412_130000"
            proc_a = run_dir / "processed" / "proc_20260412_130100"
            proc_b = run_dir / "processed" / "proc_20260412_130200"
            proc_a.mkdir(parents=True)
            proc_b.mkdir(parents=True)
            (run_dir / "raw_video.mp4").write_bytes(b"raw")
            (proc_a / "annotated_video.mp4").write_bytes(b"old_video")
            (proc_a / "report.pdf").write_bytes(b"old_pdf")
            (proc_b / "annotated_video.mp4").write_bytes(b"new_video")
            (proc_b / "report.pdf").write_bytes(b"new_pdf")
            save_json(run_dir / "processed" / "latest_processing.json", {"processing_dir": str(proc_b)})

            core_files = collect_artifacts(run_dir, mode="raw+annotated+pdf", include_backgrounds=False)
            core_names = {path.relative_to(run_dir).as_posix() for path in core_files}
            self.assertEqual(core_names, {
                "raw_video.mp4",
                "processed/proc_20260412_130200/annotated_video.mp4",
                "processed/proc_20260412_130200/report.pdf",
            })

    def test_legacy_box_settings_seed_templates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            repo_root.mkdir()
            (repo_root / "capture.py").write_text(
                "CLIENT_ID = 'abc123'\n"
                "CLIENT_SECRET = 'secret456'\n"
                "BOX_PARENT_FOLDER_ID = 'folder789'\n"
                "BOX_FOLDER_NAME = 'pi_captures'\n",
                encoding="utf-8",
            )
            (repo_root / "box_login.py").write_text(
                "TOKENS_FILE = '/unused/elsewhere/box_tokens.json'\n",
                encoding="utf-8",
            )
            save_json(
                repo_root / "box_tokens.json",
                {"access_token": "tokenA", "refresh_token": "tokenB"},
            )

            legacy = discover_legacy_box_settings(repo_root)
            self.assertEqual(legacy["client_id"], "abc123")
            self.assertEqual(legacy["parent_folder_id"], "folder789")
            self.assertEqual(Path(legacy["tokens_file"]), repo_root / "box_tokens.json")

            output_dir = Path(tmpdir) / "out"
            result = write_box_templates(output_dir, legacy_repo_root=repo_root)
            config = load_json(Path(result["config_file"]))
            self.assertEqual(config["client_id"], "abc123")
            self.assertEqual(config["client_secret"], "secret456")
            self.assertEqual(config["parent_folder_id"], "folder789")
            self.assertEqual(config["tokens_file"], str(Path(result["tokens_file"])))
            self.assertTrue(Path(result["tokens_file"]).exists())
            effective = resolve_effective_box_settings({"enabled": True}, legacy_repo_root=repo_root)
            self.assertEqual(effective.client_id, "abc123")
            self.assertEqual(effective.parent_folder_id, "folder789")
            self.assertEqual(Path(effective.tokens_file), repo_root / "box_tokens.json")

            stale_defaults = resolve_effective_box_settings(
                {
                    "enabled": True,
                    "config_file": str(Path(tmpdir) / ".config" / "fin6" / "box_config.json"),
                    "tokens_file": str(Path(tmpdir) / ".config" / "fin6" / "box_tokens.json"),
                },
                legacy_repo_root=repo_root,
            )
            self.assertEqual(Path(stale_defaults.tokens_file), repo_root / "box_tokens.json")

    def test_placeholder_config_falls_back_to_legacy_tokens_and_credentials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            repo_root.mkdir()
            (repo_root / "capture.py").write_text(
                "CLIENT_ID = 'legacy_id'\n"
                "CLIENT_SECRET = 'legacy_secret'\n"
                "BOX_PARENT_FOLDER_ID = 'legacy_folder'\n",
                encoding="utf-8",
            )
            save_json(
                repo_root / "box_tokens.json",
                {"access_token": "legacy_access", "refresh_token": "legacy_refresh"},
            )

            fin6_cfg = Path(tmpdir) / ".config" / "fin6"
            fin6_cfg.mkdir(parents=True)
            config_path = fin6_cfg / "box_config.json"
            tokens_path = fin6_cfg / "box_tokens.json"
            save_json(
                config_path,
                {
                    "enabled": True,
                    "client_id": "PASTE_BOX_CLIENT_ID_HERE",
                    "client_secret": "PASTE_BOX_CLIENT_SECRET_HERE",
                    "parent_folder_id": "PASTE_BOX_PARENT_FOLDER_ID_HERE",
                    "tokens_file": str(tokens_path),
                },
            )
            save_json(
                tokens_path,
                {
                    "access_token": "PASTE_BOX_ACCESS_TOKEN_HERE",
                    "refresh_token": "PASTE_BOX_REFRESH_TOKEN_HERE",
                },
            )

            effective = resolve_effective_box_settings({"config_file": str(config_path)}, legacy_repo_root=repo_root)
            self.assertEqual(effective.client_id, "legacy_id")
            self.assertEqual(effective.client_secret, "legacy_secret")
            self.assertEqual(effective.parent_folder_id, "legacy_folder")
            self.assertEqual(Path(effective.tokens_file), repo_root / "box_tokens.json")

            resolved = resolve_box_config({"config_file": str(config_path)}, legacy_repo_root=repo_root)
            self.assertEqual(resolved.tokens_file, repo_root / "box_tokens.json")

    def test_placeholder_tokens_raise_clear_error_without_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "box_config.json"
            tokens_path = Path(tmpdir) / "box_tokens.json"
            save_json(
                config_path,
                {
                    "enabled": True,
                    "client_id": "real_client_id",
                    "client_secret": "real_client_secret",
                    "parent_folder_id": "12345",
                    "tokens_file": str(tokens_path),
                },
            )
            save_json(
                tokens_path,
                {
                    "access_token": "PASTE_BOX_ACCESS_TOKEN_HERE",
                    "refresh_token": "PASTE_BOX_REFRESH_TOKEN_HERE",
                },
            )
            no_legacy_root = Path(tmpdir) / "no_legacy"
            no_legacy_root.mkdir()
            with self.assertRaises(BoxUploadError) as ctx:
                resolve_box_config({"config_file": str(config_path)}, legacy_repo_root=no_legacy_root)
            self.assertIn("does not contain a usable access/refresh token pair", str(ctx.exception))

    def test_legacy_client_credentials_replace_mismatched_template_token_bundle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            repo_root.mkdir()
            (repo_root / "capture.py").write_text(
                "CLIENT_ID = 'legacy_id'\n"
                "CLIENT_SECRET = 'legacy_secret'\n"
                "BOX_PARENT_FOLDER_ID = 'legacy_folder'\n",
                encoding="utf-8",
            )
            save_json(
                repo_root / "box_tokens.json",
                {"access_token": "legacy_access", "refresh_token": "legacy_refresh"},
            )

            cfg_dir = Path(tmpdir) / ".config" / "fin6"
            cfg_dir.mkdir(parents=True)
            config_path = cfg_dir / "box_config.json"
            tokens_path = cfg_dir / "box_tokens.json"
            save_json(
                config_path,
                {
                    "enabled": True,
                    "client_id": "different_client_id",
                    "client_secret": "different_client_secret",
                    "parent_folder_id": "custom_folder",
                    "tokens_file": str(tokens_path),
                },
            )
            save_json(
                tokens_path,
                {
                    "access_token": "PASTE_BOX_ACCESS_TOKEN_HERE",
                    "refresh_token": "PASTE_BOX_REFRESH_TOKEN_HERE",
                },
            )
            resolved = resolve_box_config({"config_file": str(config_path)}, legacy_repo_root=repo_root)
            self.assertEqual(resolved.client_id, "legacy_id")
            self.assertEqual(resolved.client_secret, "legacy_secret")
            self.assertEqual(resolved.parent_folder_id, "custom_folder")
            self.assertEqual(resolved.tokens_file, repo_root / "box_tokens.json")


if __name__ == "__main__":
    unittest.main()
