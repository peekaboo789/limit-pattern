import XCTest
@testable import LimitPattern

private final class CouncilTestKeychain: KeychainStoring {
    private(set) var values: [String: String] = [:]
    func set(_ value: String?, for key: String) { values[key] = value }
    func get(_ key: String) -> String? { values[key] }
    func remove(_ key: String) { values[key] = nil }
}

private final class MockURLProtocol: URLProtocol {
    static var handler: ((URLRequest) -> (Int, Data?))?
    static var delay: TimeInterval = 0
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        if Self.delay > 0 { Thread.sleep(forTimeInterval: Self.delay) }
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        let (status, data) = handler(request)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if let data { client?.urlProtocol(self, didLoad: data) }
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

private func mockAPI(_ handler: @escaping (URLRequest) -> (Int, Data?)) -> APIClient {
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [MockURLProtocol.self]
    let api = APIClient(storage: .shared, keychain: CouncilTestKeychain(), session: URLSession(configuration: config))
    api.baseURL = URL(string: "https://server.example")
    MockURLProtocol.handler = handler
    return api
}

@MainActor
final class CouncilStoreTests: XCTestCase {
    func testServerIdentitySwitchDropsLateHistoryResponse() async {
        let api = mockAPI { req in
            if req.url?.path == "/api/council/history" {
                return (200, Data(#"{"items":[{"id":1,"created_at":"2026-11-06T10:00:00+08:00"}]}"#.utf8))
            }
            return (404, Data(#"{"detail":"missing"}"#.utf8))
        }
        MockURLProtocol.delay = 0.05
        defer { MockURLProtocol.delay = 0 }
        let store = CouncilStore()
        store.api = api
        store.rebind(server: "old", account: "old+alice")
        let lateLoad = Task { await store.loadSessions() }
        try? await Task.sleep(for: .milliseconds(10))
        store.rebind(server: "new", account: "new+bob")
        await lateLoad.value
        XCTAssertEqual(store.sessions.count, 0)
    }

    func testRunResponseForOldIdentityIsIgnored() async {
        let api = mockAPI { _ in
            let json = #"{"session_id":7,"started":true,"participants":[]}"#
            return (200, Data(json.utf8))
        }
        MockURLProtocol.delay = 0.05
        defer { MockURLProtocol.delay = 0 }
        let store = CouncilStore()
        store.api = api
        store.rebind(server: "old", account: "old+alice")
        store.order = ["a", "b"]
        store.models = [.object(["id": .string("a"), "api_key_set": .bool(true)]), .object(["id": .string("b"), "api_key_set": .bool(true)])]
        // Switch identity while request is in flight; guard must not write session into new identity store.
        let runTask = Task { await store.run(code: "600519", date: nil) }
        try? await Task.sleep(for: .milliseconds(10))
        store.rebind(server: "new", account: "new+bob")
        let ok = await runTask.value
        XCTAssertFalse(ok)
        XCTAssertNil(store.activeSessionID)
    }

    func testDeleteCancelDoesNotRemove() async {
        let api = mockAPI { _ in (409, Data(#"{"detail":"运行中的讨论不能删除，请等待结束"}"#.utf8)) }
        let store = CouncilStore()
        store.api = api
        store.rebind(server: "s", account: "s+alice")
        store.sessions = [.object(["id": .number(3), "status": .string("done")])]
        let removed = await store.deleteSession(id: 3)
        XCTAssertFalse(removed)
        XCTAssertEqual(store.sessions.count, 1)
    }

    func testUserMessageErrorKeepsDraft() async {
        let api = mockAPI { _ in (409, Data(#"{"detail":"这场讨论已经失败，不能继续发言"}"#.utf8)) }
        let store = CouncilStore()
        store.api = api
        store.rebind(server: "s", account: "s+alice")
        await store.addUserMessage(id: 1, text: "我的意见")
        XCTAssertNotNil(store.detailError[1])
    }

    func testPollCancellationStopsWithoutCrashing() async {
        let api = mockAPI { _ in (500, Data(#"{"detail":"boom"}"#.utf8)) }
        let store = CouncilStore()
        store.api = api
        store.rebind(server: "s", account: "s+alice")
        store.progress = .object(["running": .bool(true)])
        store.startPoll()
        store.cancelPoll()
        XCTAssertNotNil(store.progress)
    }
}
