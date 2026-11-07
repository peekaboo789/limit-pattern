import XCTest
@testable import LimitPattern

final class CouncilCoreTests: XCTestCase {
    func testPublicContentStripsThink() {
        XCTAssertEqual(CouncilCore.publicContent("<think>secret\nchain</think>\n公开分析"), "公开分析")
        XCTAssertEqual(CouncilCore.publicContent("<THINK>x</THINK>y"), "y")
        XCTAssertEqual(CouncilCore.publicContent("<think>unclosed"), "unclosed")
        XCTAssertEqual(CouncilCore.publicContent("plain"), "plain")
    }

    func testProbabilityUnitsAreExplicitNeverBlindMultiply() {
        let est = JSON.object(["up_probability": .number(55.0), "probability_unit": .string("percent"), "probability_status": .string("estimated")])
        XCTAssertEqual(CouncilCore.probabilityText(est), "约 55.0%（主观估计）")
        let ambiguous = JSON.object(["up_probability": .number(0.3), "probability_status": .string("ambiguous_unit")])
        XCTAssertEqual(CouncilCore.probabilityText(ambiguous), "单位不明 · 不可确定")
        let invalid = JSON.object(["probability_status": .string("invalid")])
        XCTAssertEqual(CouncilCore.probabilityText(invalid), "输出无效 · 不可估计")
        let insufficient = JSON.object(["probability_status": .string("insufficient_evidence")])
        XCTAssertEqual(CouncilCore.probabilityText(insufficient), "证据不足 / 不可估计")
    }

    func testMessageDedupUsesTranscriptSeq() {
        let transcript: [JSON] = [
            .object(["seq": .number(1), "kind": .string("model"), "speaker": .string("甲"), "content": .string("a")]),
            .object(["seq": .number(1), "kind": .string("model"), "speaker": .string("甲"), "content": .string("a")]),
            .object(["seq": .number(2), "kind": .string("user"), "content": .string("意见")])
        ]
        let row = JSON.object(["id": .number(9), "created_at": .string("2026-11-06T10:00:00+08:00"), "transcript": .array(transcript)])
        let messages = CouncilCore.chatMessages(row)
        XCTAssertEqual(messages.filter { if case .speech = $0 { return true } else { return false } }.count, 2)
    }

    func testIMEAndWhitespaceGuard() {
        XCTAssertFalse(CouncilCore.shouldSend("", isComposing: false, sinceCompositionMs: 1000))
        XCTAssertFalse(CouncilCore.shouldSend("中文", isComposing: true, sinceCompositionMs: 1000))
        XCTAssertFalse(CouncilCore.shouldSend("中文", isComposing: false, sinceCompositionMs: 50))
        XCTAssertTrue(CouncilCore.shouldSend("中文", isComposing: false, sinceCompositionMs: 101))
    }

    func testNearBottom() {
        XCTAssertTrue(CouncilCore.nearBottom(contentHeight: 300, offsetY: 210, viewportHeight: 100))
        XCTAssertFalse(CouncilCore.nearBottom(contentHeight: 600, offsetY: 0, viewportHeight: 100))
    }

    func testEvidenceQualityAndMissingNotZero() {
        let evidence = JSON.object(["kline": .object(["local_count": .number(0), "public_count": .number(0), "expected_trading_days": .number(30), "missing_reason": .string("无法补齐")])])
        let summary = CouncilCore.evidenceSummary(evidence)
        XCTAssertTrue(summary.contains("0 根"))
        XCTAssertTrue(summary.contains("无法补齐"))
    }

    func testSourceHealthCircuitText() {
        let future = JSON.object(["health": .object(["status": .string("ok"), "open_until": .number(Date().timeIntervalSince1970 + 100)])])
        XCTAssertEqual(CouncilCore.sourceHealthText(future), "熔断冷却中")
    }
}
