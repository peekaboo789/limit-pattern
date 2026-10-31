import XCTest
@testable import LimitPattern

final class APITests: XCTestCase {
    func testJSONDecoding() throws {
        let raw = #"{"date":"2026-10-30","items":[{"code":"002365","composite_score":64.22,"is_limit_up":true,"tags":["首板"],"model_score":null}]}"#
        let j = try JSON.parse(Data(raw.utf8))
        XCTAssertEqual(j["date"].string, "2026-10-30")
        XCTAssertEqual(j["items"][0]["code"].text, "002365")
        XCTAssertEqual(j["items"][0]["composite_score"].fmt(2), "64.22")
        XCTAssertTrue(j["items"][0]["is_limit_up"].bool)
        XCTAssertEqual(j["items"][0]["model_score"].text, "—")
        XCTAssertEqual(j["missing"]["x"], .null)
    }
    func testNormalizeURL() {
        XCTAssertEqual(APIClient.normalize("192.168.1.10:8000/")?.absoluteString, "http://192.168.1.10:8000")
        XCTAssertEqual(APIClient.normalize("https://a.b.com/api")?.absoluteString, "https://a.b.com")
        XCTAssertNil(APIClient.normalize("  "))
    }
    func testVersionCompare() {
        XCTAssertEqual(APIClient.compareVersion("0.4.8", "0.5.0"), -1)
        XCTAssertEqual(APIClient.compareVersion("0.5.1", "0.5.0"), 1)
        XCTAssertEqual(APIClient.compareVersion("0.5", "0.5.0"), 0)
    }
    func testContentDisposition() {
        XCTAssertEqual(APIClient.filename(from: "attachment; filename*=UTF-8''%E8%87%AA%E9%80%89.txt"), "自选.txt")
        XCTAssertEqual(APIClient.filename(from: "attachment; filename=\"a.csv\""), "a.csv")
    }
    func testCookieRestore() {
        let storage = HTTPCookieStorage.sharedCookieStorage(forGroupContainerIdentifier: "test.\(UUID().uuidString)")
        let api = APIClient(storage: storage)
        api.baseURL = URL(string: "http://10.0.0.2:8000")
        XCTAssertNil(api.sessionCookie())
        let c = APIClient.makeCookie(value: "abc.123", for: api.baseURL!)!
        storage.setCookie(c)
        XCTAssertEqual(api.sessionCookie()?.value, "abc.123")
        api.clearSession()
        XCTAssertNil(api.sessionCookie())
    }
    func testPctColorText() {
        XCTAssertEqual(Theme.pctText(9.98), "+9.98%")
        XCTAssertEqual(Theme.pctText(-0.1), "-0.10%")
    }
}
