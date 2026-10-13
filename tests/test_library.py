import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import library


class TemplateTests(unittest.TestCase):
    def test_groups_vars_and_escapes(self):
        out = library.expand("$1 [${workflow-tag}] $$5 ${lb}x${rb}", {"workflow-tag": "Up"}, ("Show",))
        self.assertEqual(out, "Show [Up] $5 {x}")

    def test_unknown_variable_raises(self):
        with self.assertRaises(library.TemplateError):
            library.expand("${nope}", {})

    def test_partial_keeps_token_and_reescapes_dollars(self):
        partial = library.expand("$1 [${output-vresmode}]", {}, ("Cost $5",), keep=("output-vresmode",))
        self.assertEqual(partial, "Cost $$5 [${output-vresmode}]")
        self.assertEqual(library.final_name(partial, "1080p"), "Cost $5 [1080p].mkv")

    def test_beavis_tag_swap(self):
        wf = {"base_regex": r"^(.*) \[Original\](.*)\.[^.]+$",
              "target_format": "$1 [${workflow-tag} ${output-vresmode}]$2${ext}", "workflow_tag": "Upscaled"}
        tmpl = library.name_template(wf, Path("Z:/x/Lightning Strikes [Original] [TUKTC].mkv"))
        self.assertEqual(library.final_name(tmpl, "1080p"), "Lightning Strikes [Upscaled 1080p] [TUKTC].mkv")

    def test_safe_filename_forces_mkv(self):
        self.assertEqual(library.safe_filename("a: b.mp4"), "a_ b.mkv")
        self.assertEqual(library.safe_filename("no ext"), "no ext.mkv")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.child = self.root / "Series" / "Show"
        self.child.mkdir(parents=True)

    def write(self, d, data):
        (d / library.SETTINGS_NAME).write_text(json.dumps(data), encoding="utf-8")

    def test_override_chain(self):
        self.write(self.root, {"workflows": {"Custom": {"workflow_tag": "Root"},
                                             "Norm": {"upscale_enabled": False}}})
        self.write(self.child, {"workflows": {"Norm": {"show_regex": r"\[DVD\]"}}})
        res = library.resolve(self.child)
        norm = res["workflows"]["Norm"]
        self.assertFalse(norm["upscale_enabled"])          # inherited from root
        self.assertEqual(norm["show_regex"], r"\[DVD\]")    # overridden in child
        self.assertEqual(norm["workflow_tag"], "Root")      # root's Custom is the base
        self.assertEqual(norm["defined_in"], str(self.child))
        self.assertEqual(res["workflows"]["Custom"]["workflow_tag"], "Root")

    def test_ignore_parent_settings(self):
        self.write(self.root, {"workflows": {"Norm": {"upscale_enabled": False}}})
        self.write(self.child, {"ignore_parent_settings": True})
        res = library.resolve(self.child)
        self.assertNotIn("Norm", res["workflows"])
        self.assertEqual(res["chain"], [str(self.child)])

    def test_save_workflow_stores_only_diff(self):
        self.write(self.root, {"workflows": {"Norm": {"upscale_enabled": False}}})
        fields = dict(library.resolve(self.child)["workflows"]["Norm"])
        fields["workflow_tag"] = "Normalized"
        library.save_workflow(str(self.child), "Norm", fields)
        local = library.read_local(self.child)
        self.assertEqual(local["workflows"]["Norm"], {"workflow_tag": "Normalized"})

    def test_rename_inherited_refused(self):
        self.write(self.root, {"workflows": {"Norm": {}}})
        with self.assertRaises(library.SettingsError):
            library.rename_workflow(str(self.child), "Norm", "Other")

    def test_bad_regex_rejected(self):
        with self.assertRaises(library.SettingsError):
            library.write_local(self.child, {"workflows": {"X": {"show_regex": "("}}})


class PlanAndStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name) / "Disc 1"
        self.dir.mkdir()
        self.src = self.dir / "Ep1 [DVD].mkv"
        self.src.write_bytes(b"x")
        self.wf = {**library.app_default_workflow(), "output_dir": "..", "workflow_tag": "Up"}
        for target, kwargs in [("db.list_jobs", {"return_value": []}),
                               ("probe_media", {"return_value": {}}),
                               ("get_roots", {"return_value": [self.temp.name]})]:
            p = patch.object(library, target.split(".")[-1], **kwargs) if "." not in target \
                else patch(f"app.library.{target}", **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def test_relative_output_dir_and_conflicts(self):
        plans = library.plan_outputs([str(self.src)], self.wf, str(self.dir))
        expected = os.path.join(os.path.normpath(self.temp.name), "Ep1 [DVD] [Up 1080p].mkv")
        self.assertEqual(plans[0]["output_path"], expected)
        library.find_conflicts(plans)
        self.assertIsNone(plans[0]["conflict"])
        Path(expected).write_bytes(b"y")
        library.find_conflicts(plans)
        self.assertIn("exists", plans[0]["conflict"])

    def test_filename_fallback_marks_completed(self):
        (Path(self.temp.name) / "Ep1 [DVD] [Up 720p].mkv").write_bytes(b"y")
        status = library.completion_status([str(self.src)], self.wf, str(self.dir))
        self.assertTrue(status[str(self.src)]["completed"])
        self.assertEqual(status[str(self.src)]["via"], "filename")

    def test_metadata_match_requires_same_stages(self):
        out = Path(self.temp.name) / "renamed.mkv"
        out.write_bytes(b"y")
        tags = {"SOURCE_FILE": self.src.name, "WORKFLOW_STAGES": "deinterlace", "WORKFLOW_TAG": "Up"}
        with patch.object(library, "probe_media", return_value={"tags": tags}):
            self.assertFalse(library.completion_status([str(self.src)], self.wf, str(self.dir))[str(self.src)]["completed"])
            tags["WORKFLOW_STAGES"] = "deinterlace,upscale"
            status = library.completion_status([str(self.src)], self.wf, str(self.dir))[str(self.src)]
        self.assertEqual(status["via"], "metadata")


if __name__ == "__main__":
    unittest.main()
