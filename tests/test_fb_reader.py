import html, json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fb_reader"))
import fb_reader


def page(nodes, og=True):
    data = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"result": {"data": {"user": {
        "timeline_list_feed_units": {"edges": [{"node": n} for n in nodes]}}}}}}]]]}
    meta = '<meta property="og:title" content="Some Page" />' if og else ""
    return (f"<html><head>{meta}</head><body>"
            f'<script type="application/json" data-sjs>{html.escape(json.dumps(data), quote=False)}</script>'
            "</body></html>")


def node(pid, text, ts):
    return {"post_id": pid, "creation_time": ts,
            "comet_sections": {"content": {"story": {"wwwURL": f"https://www.facebook.com/x/posts/{pid}",
                                                     "message": {"text": text}}}},
            "attachments": [{"styles": {"attachment": {"media": {"photo_image": {"uri": "https://scontent/x.jpg"}}}}}]}


class ExtractTest(unittest.TestCase):
    def test_posts(self):
        now = int(time.time())
        posts = fb_reader.extract_posts(page([node("1", "old", now - 86400 * 3), node("2", "Monday\nA: 5x5", now)]), "x")
        posts.sort(key=lambda p: p["created"], reverse=True)
        p = fb_reader.enrich(posts[0])
        self.assertEqual(p["id"], "2")
        self.assertEqual(p["images"], ["https://scontent/x.jpg"])
        self.assertTrue(p["is_today"])
        self.assertFalse(fb_reader.enrich(posts[1])["is_today"])

    def test_login_wall(self):
        with self.assertRaises(fb_reader.Blocked):
            fb_reader.extract_posts(page([], og=False), "x")

    def test_atom_is_valid_xml(self):
        p = fb_reader.enrich(fb_reader.extract_posts(page([node("9", "Tues & <stuff>", int(time.time()))]), "x")[0])
        fb_reader.states["x"] = {"post": p, "checked": None}
        import xml.dom.minidom
        xml.dom.minidom.parseString(fb_reader.atom("x"))


class OptionsTest(unittest.TestCase):
    def test_pages(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"poll_minutes": 1, "pages": [
                {"page": "https://www.facebook.com/1884FunctionalFitness/"},
                {"page": "NASA", "token": " t ", "sensor": "space"},
                {"page": ""}]}, f)
        fb_reader.OPTIONS_FILE = f.name
        pages, minutes = fb_reader.load_options()
        os.unlink(f.name)
        self.assertEqual(minutes, 5)
        self.assertEqual([p["page"] for p in pages], ["1884FunctionalFitness", "NASA"])
        self.assertEqual(pages[0]["sensor"], "sensor.fb_1884functionalfitness")
        self.assertEqual(pages[1]["sensor"], "sensor.fb_space")
        self.assertEqual(pages[1]["token"], "t")


unittest.main()
