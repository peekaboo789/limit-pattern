import XCTest
@testable import LimitPattern

private final class BatchTestKeychain: KeychainStoring {
    private(set) var values: [String: String] = [:]
    func set(_ value: String?, for key: String) { values[key] = value }
    func get(_ key: String) -> String? { values[key] }
    func remove(_ key: String) { values[key] = nil }
}

private final class BatchMockURLProtocol: URLProtocol {
    static var handler: ((URLRequest) -> (Int, Data?))?
    static var delay: TimeInterval = 0
    static var seenRequests: [String] = []
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        if Self.delay > 0 { Thread.sleep(forTimeInterval: Self.delay) }
        Self.seenRequests.append(Self.requestKey(request))
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
    static func requestKey(_ request: URLRequest) -> String {
        "\(request.httpMethod ?? "") \(request.url?.path ?? "")"
    }
}

private func batchMockAPI(_ handler: @escaping (URLRequest) -> (Int, Data?)) -> APIClient {
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [BatchMockURLProtocol.self]
    let api = APIClient(storage: .shared, keychain: BatchTestKeychain(), session: URLSession(configuration: config))
    api.baseURL = URL(string: "https://server.example")
    BatchMockURLProtocol.handler = handler
    BatchMockURLProtocol.seenRequests = []
    return api
}

private func sampleBatch() -> JSON {
    JSON.object([
        "id": .number(11), "source_name": .string("ranking.txt"), "source_date": .string("2026-11-06"),
        "eval_date": .string("2026-11-06"), "status": .string("pending"), "stage": .string("待开始"),
        "profile_ids": .array([.string("p1"), .string("p2")]),
        "item_count": .number(2), "done_count": .number(0), "failed_count": .number(0), "cancelled_count": .number(0),
        "parser_summary": .object([:]), "error": .string(""), "created_at": .null, "updated_at": .null,
        "started_at": .null, "finished_at": .null,
        "items": .array([
            .object(["id": .number(1), "batch_id": .number(11), "position": .number(1), "stock_code": .string("000001"), "stock_name": .string("平安银行"), "status": .string("pending"), "stage": .string("待开始"), "session_id": .null, "probability_percent": .null, "confidence": .string(""), "summary": .string(""), "error": .string(""), "details": .object([:])]),
            .object(["id": .number(2), "batch_id": .number(11), "position": .number(2), "stock_code": .string("600519"), "stock_name": .string("贵州茅台"), "status": .string("pending"), "stage": .string("待开始"), "session_id": .null, "probability_percent": .null, "confidence": .string(""), "summary": .string(""), "error": .string(""), "details": .object([:])])
        ])
    ])
}

@MainActor
final class CouncilBatchStoreTests: XCTestCase {
    func testStartPayloadUsesSelectedItemsAndStableIdempotencyKey() async {
        let api = batchMockAPI { request in
            if request.url?.path == "/api/council/batch/start" {
                let object = try! JSONSerialization.jsonObject(with: request.httpBody ?? Data())
                let json = JSON.from(object)
                XCTAssertEqual(json["eval_date"].string, "2026-11-06")
                XCTAssertEqual(json["profile_ids"].array.count, 2)
                XCTAssertEqual(json["preview"]["items"].array.filter { $0["selected"].bool }.count, 1)
                XCTAssertNotNil(json["idempotency_key"].string)
                return (200, try! JSONSerialization.data(withJSONObject: sampleBatch().any))
            }
            return (404, Data(#"{"detail":"missing"}"#.utf8))
        }
        let store = CouncilBatchStore()
        store.api = api
        store.rebind(server: "s", account: "s+alice")
        store.preview = try! CouncilBatchCore.preview(text: "数据日期\t2026-11-06\n排名\t代码\t名称\n1\t000001\t平安银行\n2\t600519\t贵州茅台\n", sourceName: "r.txt")
        store.selectedEvalDate = "2026-11-06"
        let ok = await store.start(profileIDs: ["p1", "p2"], evalDate: "2026-11-06", selectedCodes: Set(["000001"]))
        XCTAssertTrue(ok)
        XCTAssertEqual(store.batches.first?.councilID, 11)
    }

    func testPreviewRawBytesAndFileNameHeader() async {
        let api = batchMockAPI { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "x-file-name"), "ranking.txt")
            XCTAssertFalse((request.value(forHTTPHeaderField: "Content-Type") ?? "").contains("json"))
            return (200, try! JSONSerialization.data(withJSONObject: sampleBatch().any))
        }
        let store = CouncilBatchStore()
        store.api = api
        store.rebind(server: "s", account: "s+alice")
        let data = Data("数据日期\t2026-11-06\n排名\t代码\t名称\n1\t000001\t平安银行\n".utf8)
        _ = await store.fetchPreview(data: data, fileName: "ranking.txt")
        XCTAssertEqual(store.selectedEvalDate, "2026-11-06")
    }

    func testIdentitySwitchDropsLatePreviewResponse() async {
        let api = batchMockAPI { _ in (200, try! JSONSerialization.data(withJSONObject: sampleBatch().any)) }
        BatchMockURLProtocol.delay = 0.05
        defer { BatchMockURLProtocol.delay = 0 }
        let store = CouncilBatchStore()
        store.api = api
        store.rebind(server: "old", account: "old+alice")
        let task = Task { await store.fetchPreview(data: Data("x".utf8), fileName: "x.txt") }
        try? await Task.sleep(for: .milliseconds(10))
        store.rebind(server: "new", account: "new+bob")
        let ok = await task.value
        XCTAssertFalse(ok)
        XCTAssertTrue(store.preview.isNull)
    }

    func testStartFailureDoesNotClaimSuccess() async {
        let api = batchMockAPI { _ in (409, Data(#"{"detail":"单股会话正在运行"}"#.utf8)) }
        let store = CouncilBatchStore()
        store.api = api
        store.rebind(server: "s", account: "s+alice")
        store.preview = try! CouncilBatchCore.preview(text: "数据日期\t2026-11-06\n排名\t代码\t名称\n1\t000001\t平安银行\n2\t600519\t贵州茅台\n", sourceName: "r.txt")
        let ok = await store.start(profileIDs: ["p1", "p2"], evalDate: "2026-11-06")
        XCTAssertFalse(ok)
        XCTAssertTrue(store.batches.isEmpty)
        XCTAssertNotNil(store.lastError)
    }

    func testCancelOnlyPendingAnd404Friendly() async {
        let api = batchMockAPI { _ in (200, try! JSONSerialization.data(withJSONObject: sampleBatch().any)) }
        let store = CouncilBatchStore()
        store.api = api
        store.rebind(server: "s", account: "s+alice")
        await store.cancel(id: 11)
        XCTAssertEqual(store.detailByID[11]?.councilID, 11)
    }

    func testPollCancellationStopsWithoutLeak() async {
        let api = batchMockAPI { _ in (200, try! JSONSerialization.data(withJSONObject: ["items": [sampleBatch().any]])) }
        let store = CouncilBatchStore()
        store.api = api
        store.rebind(server: "s", account: "s+alice")
        store.batches = [sampleBatch()]
        store.startPollIfNeeded()
        store.cancelPoll()
        XCTAssertFalse(store.runningBatchIDs.isEmpty == false)
    }

    func testBatch404ShowsUpgradeMessage() async {
        let api = batchMockAPI { _ in (404, Data(#"{"detail":"Not Found"}"#.utf8)) }
        let store = CouncilBatchStore()
        store.api = api
        store.rebind(server: "s", account: "s+alice")
        await store.loadBatches(serverVersion: "0.7.18")
        XCTAssertFalse(store.serverSupportsBatch)
        XCTAssertEqual(store.lastError, CouncilBatchCore.batchUpgradeMessage)
    }
}
