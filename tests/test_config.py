import json
import tempfile
import unittest
from pathlib import Path

from fb_automator.config import load_groups


class GroupConfigTests(unittest.TestCase):
    def test_loads_facebook_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "groups.json"
            path.write_text(
                json.dumps(
                    [{"name": "Housing", "url": "https://www.facebook.com/groups/123/"}]
                ),
                encoding="utf-8",
            )
            groups = load_groups(path)
        self.assertEqual(groups[0].name, "Housing")

    def test_rejects_non_group_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "groups.json"
            path.write_text(
                json.dumps([{"name": "Not a group", "url": "https://example.com/"}]),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_groups(path)


if __name__ == "__main__":
    unittest.main()
