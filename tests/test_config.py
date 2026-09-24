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
                    [{
                        "name": "Housing",
                        "url": "https://www.facebook.com/groups/123/",
                        "city": "amsterdam",
                    }]
                ),
                encoding="utf-8",
            )
            groups = load_groups(path)
        self.assertEqual(groups[0].name, "Housing")
        self.assertEqual(groups[0].city, "amsterdam")

    def test_rejects_non_group_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "groups.json"
            path.write_text(
                json.dumps([{
                    "name": "Not a group",
                    "url": "https://example.com/",
                    "city": "lausanne",
                }]),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_groups(path)

    def test_rejects_unknown_city(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "groups.json"
            path.write_text(
                json.dumps([{
                    "name": "Housing",
                    "url": "https://www.facebook.com/groups/123/",
                    "city": "rotterdam",
                }]),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_groups(path)


if __name__ == "__main__":
    unittest.main()
