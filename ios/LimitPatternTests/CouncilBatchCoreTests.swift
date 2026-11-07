import XCTest
@testable import LimitPattern

final class CouncilBatchCoreTests: XCTestCase {
    private func sampleData(bom: Bool = false) -> Data {
        let body = "数据日期\t2026-11-06\n排名\t代码\t名称\t板块\t最相似样本\n1\t000001\t平安银行\t银行\tA\n2\t600519\t贵州茅台\t白酒\tB\n3\t000001\t平安银行重复\t银行\tA\n4\t601899\t紫金矿业\t有色\tC\n5\t300750\t宁德时代\t创业板\tD\n"
        return Data((bom ? "\u{FEFF}" : "") + body, using: .utf8)!
    }

    func testPreviewPreservesLeadingZerosAndDateAndSkipsSimilarity() throws {
        let json = try CouncilBatchCore.preview(sampleData(bom: true), sourceName: "ranking.txt")
        XCTAssertEqual(json["source_date"].string, "2026-11-06")
        XCTAssertEqual(json["default_eval_date"].string, "2026-11-06")
        XCTAssertEqual(json["counts"]["valid"].int, 4)
        XCTAssertEqual(json["counts"]["invalid"].int, 1)
        XCTAssertEqual(json["items"][0]["code"].string, "000001")
        XCTAssertNil(json["items"][0]["code"].string.flatMap { Int($0) }) // 前导 0 不能丢
        XCTAssertTrue(json["errors"].array.contains { $0["reason"].string?.contains("重复") == true })
    }

    func testTrapsTooLargeAndTooManyCandidatesAndBadEncoding() {
        XCTAssertThrowsError(try CouncilBatchCore.decode(Data(repeating: 0x41, count: CouncilBatchCore.maxUploadBytes + 1))) { error in
            XCTAssertEqual(error as? CouncilBatchParseError, .fileTooLarge(CouncilBatchCore.maxUploadBytes))
        }
        let badEncoding = Data([0xFF, 0xFE, 0x41, 0x00])
        XCTAssertThrowsError(try CouncilBatchCore.decode(badEncoding)) { error in
            XCTAssertEqual(error as? CouncilBatchParseError, .unsupportedText)
        }
        var rows = ["数据日期\t2026-11-06", "排名\t代码\t名称"]
        for index in 0..<110 {
            rows.append("\(index + 1)\t600000\t测试\(index)")
        }
        let text = rows.joined(separator: "\n")
        let json = try! CouncilBatchCore.preview(text: text, sourceName: "many.txt")
        XCTAssertEqual(json["counts"]["total"].int, 100)
        XCTAssertTrue(json["errors"].last?["reason"].string?.contains("超过") == true)
    }

    func testIdempotencyKeyDependsOnSelectedCodesAndProfileOrder() throws {
        let json = try CouncilBatchCore.preview(sampleData(), sourceName: "ranking.txt")
        var items = json["items"].array
        items[0] = .object(items[0].object.merging(["selected": .bool(false)]) { _, new in new })
        let unselected = json.object.merging(["items": .array(items)]) { _, new in new }
        let all = CouncilBatchCore.idempotencyKey(preview: json, evalDate: "2026-11-06", profileIDs: ["a", "b"])
        let subset = CouncilBatchCore.idempotencyKey(preview: JSON.object(unselected), evalDate: "2026-11-06", profileIDs: ["a", "b"])
        let reversed = CouncilBatchCore.idempotencyKey(preview: json, evalDate: "2026-11-06", profileIDs: ["b", "a"])
        XCTAssertNotEqual(all, subset)
        XCTAssertNotEqual(all, reversed)
        XCTAssertEqual(all, CouncilBatchCore.idempotencyKey(preview: json, evalDate: "2026-11-06", profileIDs: ["a", "b"]))
    }

    func testProbabilityDoesNotBlindMultiplyOrFillMissing() {
        let raw = JSON.object(["probability_percent": .number(0.3)])
        XCTAssertEqual(CouncilBatchCore.batchProbabilityText(raw), "约 0.3%（主观估计）")
        let missing = JSON.object(["probability_percent": .null, "details": .object(["probability_status": .string("insufficient_evidence")])])
        XCTAssertEqual(CouncilBatchCore.batchProbabilityText(missing), "证据不足 / 不可估计")
        let invalid = JSON.object(["probability_percent": .null, "details": .object(["probability_status": .string("invalid")])])
        XCTAssertEqual(CouncilBatchCore.batchProbabilityText(invalid), "输出无效 · 不可估计")
    }

    func testFileNameHeader() {
        XCTAssertEqual(CouncilBatchCore.headerFileName("ranking.txt"), "ranking.txt")
        XCTAssertEqual(CouncilBatchCore.headerFileName("中文 名单.txt"), "中文%20名单.txt")
    }
}
