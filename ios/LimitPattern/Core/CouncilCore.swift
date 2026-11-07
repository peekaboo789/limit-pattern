import Foundation

/// 多模型讨论验证的纯逻辑与契约层。所有解析、状态、安全显示、单位校验、滚动判断、
/// 消息去重、IME 发送判断都在这里，方便单元测试；不依赖 SwiftUI。
enum CouncilCore {
    static let maxUserMessageLength = 4000
    static let requiredProfileCount = 2
    static let probabilityNotice = "仅为模型主观估计，未经统计校准，不是实测上涨频率；不计入综合评分，不是投资建议。"
    static let conversationDisclaimer = "这是讨论会估计，不计入综合评分，不是涨停概率，不是买入建议。概率指下一交易日收盘相对评估日收盘上涨的主观估计，未经统计校准。单位不明或关键证据不足时不展示数字。"
    static let serverUpgradeMessage = "服务端需升级到支持讨论的版本"
}

enum CouncilChatMessage: Equatable {
    case speech(key: String, kind: String, content: String, name: String, time: String?, error: String?, model: String?, elapsed: Double?)
    case system(key: String, name: String, content: String, time: String?)
}

struct CouncilBlock: Equatable {
    enum Kind: Equatable { case text, code(language: String), table }
    let kind: Kind
    let text: String
    let head: [String]
    let rows: [[String]]
}

struct CouncilInlinePart: Equatable {
    let text: String
    let href: String?
}

enum CouncilTranscriptKind {
    static func label(_ value: String?) -> String {
        guard let value else { return "模型" }
        switch value {
        case "user": return "用户意见"
        case "chairman": return "主持人"
        default: return "模型"
        }
    }
}

extension CouncilCore {
    static func publicContent(_ text: String?) -> String {
        guard let text, !text.isEmpty else { return "" }
        let ns = text as NSString
        let open = ns.range(of: "<think>", options: .caseInsensitive)
        guard open.location != NSNotFound else { return text }
        let tail = ns.substring(from: open.location + open.length) as NSString
        let end = tail.range(of: "</think>", options: .caseInsensitive)
        if end.location != NSNotFound {
            return tail.substring(to: end.location).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return (tail as String).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func parsedInstant(_ value: String?) -> Date? {
        guard let value, !value.isEmpty else { return nil }
        let candidate = value.contains("Z") || value.contains("+") || value.contains("-") ? value : value + "Z"
        return ISO8601DateFormatter().date(from: candidate) ?? ISO8601DateFormatter().date(from: value)
    }

    static func sortedStableKeys(_ entries: [JSON]) -> [String] {
        let withSeq = entries.filter { $0["seq"].int != nil && $0["seq"].int! > 0 }
            .sorted { ($0["seq"].int ?? 0) < ($1["seq"].int ?? 0) }
        if !withSeq.isEmpty { return withSeq.map { "seq:\($0["seq"].int ?? 0)" } }
        return entries.enumerated().map { index, item in
            let identity = [item["kind"].string ?? "", item["model"].string ?? "", item["started_at"].string ?? "",
                            item["content"].string ?? "", item["error"].string ?? ""].joined(separator: "|")
            return "legacy:\(index):\(identity)"
        }
    }

    static func probabilityText(_ conclusion: JSON?) -> String {
        guard let c = conclusion else { return "—" }
        let status = c["probability_status"].string ?? "estimated"
        let unit = c["probability_unit"].string
        let value = c["up_probability"].double
        if status == "estimated", unit == "percent", let value, value.isFinite {
            return String(format: "约 %.1f%%（主观估计）", value)
        }
        switch status {
        case "not_estimable": return "模型未提供估计"
        case "ambiguous_unit": return "单位不明 · 不可确定"
        case "invalid": return "输出无效 · 不可估计"
        default: return "证据不足 / 不可估计"
        }
    }

    static func confidenceLabel(_ value: String?) -> String {
        switch value?.lowercased() {
        case "low": return "低"
        case "medium": return "中"
        case "high": return "高"
        default: return value?.isEmpty == false ? value! : "—"
        }
    }

    static func categoryLabel(_ value: String?) -> String {
        switch value {
        case "market": return "行情与图形"
        case "market_intraday": return "分时/分钟"
        case "capital": return "资金与交易"
        case "trade": return "大宗/回购/增减持/解禁"
        case "news": return "资讯快讯"
        case "announcement": return "公司公告"
        case "research": return "研报观点"
        case "financial": return "最新财报与财务分析"
        default: return value?.isEmpty == false ? value! : "其他"
        }
    }

    static func sourceStatusLabel(_ value: String?) -> String {
        switch value {
        case "ok": return "已取到"
        case "empty": return "有效查询无记录"
        case "failed": return "接口故障"
        case "not_applicable": return "已核实不适用"
        case "unavailable": return "公开数据不可得"
        case "unverified": return "尚未核验"
        default: return value?.isEmpty == false ? value! : "未知"
        }
    }

    static func joinList(_ values: [JSON]) -> String {
        let parts = values.compactMap { $0.string }.filter { !$0.isEmpty }
        return parts.isEmpty ? "—" : parts.joined(separator: "；")
    }

    static func durationText(_ seconds: Double?) -> String {
        guard let seconds else { return "" }
        let rounded = Int(seconds.rounded())
        if rounded < 60 { return "\(rounded) 秒" }
        return "\(rounded / 60) 分 \(rounded % 60) 秒"
    }

    static func displayTime(_ value: String?) -> String {
        guard let value, !value.isEmpty else { return "—" }
        guard let date = parsedInstant(value) else { return value }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "sv_SE")
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter.string(from: date)
    }

    static func nearBottom(contentHeight: Double, offsetY: Double, viewportHeight: Double) -> Bool {
        guard viewportHeight > 0 else { return true }
        return contentHeight - offsetY - viewportHeight <= 90
    }

    static func shouldSend(_ text: String, isComposing: Bool, sinceCompositionMs: Int) -> Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !isComposing && sinceCompositionMs > 100
    }

    static func clearSubmittedDraft(current: String, submitted: String) -> String {
        current == submitted ? "" : current
    }

    static func inlineParts(_ text: String) -> [CouncilInlinePart] {
        let pattern = #"\[([^\]\n]{1,200})\]\((https?:\/\/[^\s)]{1,2048})\)|(https?:\/\/[^\s<>]{1,2048})"#
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return [CouncilInlinePart(text: text, href: nil)] }
        let ns = text as NSString
        let matches = regex.matches(in: text, range: NSRange(location: 0, length: ns.length))
        var parts: [CouncilInlinePart] = []
        var end = 0
        for match in matches {
            if match.range.location > end {
                parts.append(CouncilInlinePart(text: ns.substring(with: NSRange(location: end, length: match.range.location - end)), href: nil))
            }
            let raw = ns.substring(with: match.range)
            let href = match.range(at: 2).location != NSNotFound ? ns.substring(with: match.range(at: 2))
                : (match.range(at: 3).location != NSNotFound ? ns.substring(with: match.range(at: 3)) : "")
            let label = match.range(at: 1).location != NSNotFound ? ns.substring(with: match.range(at: 1)) : href
            var safe = false
            if let url = URL(string: href), (url.scheme == "https" || url.scheme == "http"), url.user == nil, url.password == nil { safe = true }
            parts.append(CouncilInlinePart(text: safe ? label : raw, href: safe ? href : nil))
            end = match.range.location + match.range.length
        }
        if end < ns.length { parts.append(CouncilInlinePart(text: ns.substring(from: end), href: nil)) }
        return parts
    }

    static func messageBlocks(_ text: String) -> [CouncilBlock] {
        let lines = text.components(separatedBy: "\n")
        var result: [CouncilBlock] = []
        var plain: [String] = []
        func flush() { if !plain.isEmpty { result.append(CouncilBlock(kind: .text, text: plain.joined(separator: "\n"), head: [], rows: [])); plain.removeAll() } }
        func cells(_ line: String) -> [String] {
            var s = line.trimmingCharacters(in: .whitespaces)
            if s.hasPrefix("|") { s.removeFirst() }
            if s.hasSuffix("|") { s.removeLast() }
            return s.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
        }
        var i = 0
        while i < lines.count {
            let line = lines[i]
            if let fence = Self.fenceLine(line) {
                flush()
                var code: [String] = []
                i += 1
                while i < lines.count && !Self.isClosingFence(lines[i]) { code.append(lines[i]); i += 1 }
                result.append(CouncilBlock(kind: .code(language: fence), text: code.joined(separator: "\n"), head: [], rows: []))
                i += 1
                continue
            }
            let next = i + 1 < lines.count ? lines[i + 1] : nil
            if line.contains("|"), let next, cells(next).count > 1, cells(next).allSatisfy({ $0.range(of: #"^-{3,}$"#, options: .regularExpression) != nil || $0.range(of: #"^:?-{3,}:?$"#, options: .regularExpression) != nil }) {
                flush()
                let head = cells(line)
                var rows: [[String]] = []
                i += 1
                while i + 1 < lines.count && lines[i + 1].contains("|") && !lines[i + 1].trimmingCharacters(in: .whitespaces).isEmpty {
                    i += 1
                    rows.append(head.indices.map { cells(lines[i]).count > $0 ? cells(lines[i])[$0] : "" })
                }
                result.append(CouncilBlock(kind: .table, text: "", head: head, rows: rows))
                i += 1
                continue
            }
            plain.append(line)
            i += 1
        }
        flush()
        return result
    }

    private static func fenceLine(_ line: String) -> String? {
        let s = line.trimmingCharacters(in: .whitespaces)
        guard s.hasPrefix("```"), s.count > 3 || s == "```" else { return nil }
        let rest = String(s.dropFirst(3)).trimmingCharacters(in: .whitespaces)
        return String(rest.prefix(24))
    }

    private static func isClosingFence(_ line: String) -> Bool {
        line.trimmingCharacters(in: .whitespaces).hasPrefix("```")
    }

    static func transcriptItems(_ row: JSON) -> [JSON] {
        let t = row["transcript"].array
        if !t.isEmpty { return t }
        return row["statements"].array.enumerated().map { index, item in
            JSON.object([
                "seq": .number(Double(index + 1)), "kind": .string("model"), "profile_id": .null,
                "speaker": item["speaker"].isNull ? .null : .string(item["speaker"].text),
                "model": item["model"].isNull ? .null : .string(item["model"].text),
                "round": .null, "content": item["content"].isNull ? .null : .string(item["content"].text),
                "error": item["error"].isNull ? .null : .string(item["error"].text),
                "status": .string(item["error"].string?.isEmpty == false ? "failed" : "done"),
                "started_at": .null, "finished_at": .null, "elapsed_ms": .null
            ])
        }
    }

    static func chatMessages(_ row: JSON) -> [CouncilChatMessage] {
        let prefix = "\(row["id"].int ?? 0):\(row["created_at"].string ?? "legacy")"
        let entries = transcriptItems(row)
        var seen = Set<String>()
        var speech: [(seq: Double, key: String, item: JSON)] = []
        for (index, item) in entries.enumerated() {
            let seq = item["seq"].int
            let key = seq.map { "seq:\($0)" } ?? "legacy:\(index):\(item.anyHashKey)"
            if seen.contains(key) { continue }
            seen.insert(key)
            let order = seq.map(Double.init) ?? Double(index + 1)
            speech.append((order, key, item))
        }
        speech.sort { $0.seq < $1.seq }

        var events: [(time: Double, message: CouncilChatMessage)] = []
        for event in row["progress"]["events"].array {
            let key = "stage:\([event["phase"].string ?? "", event["stage"].string ?? "", event["model"].string ?? "", event["started_at"].string ?? ""].joined(separator: "|"))"
            let stage = event["stage"].string ?? event["phase"].string ?? "处理中"
            let model = event["model"].string
            let suffix = event["finished_at"].isNull ? " · 正在处理" : ""
            let content = stage + (model?.isEmpty == false ? " · \(model!)" : "") + suffix
            let t = Self.parsedInstant(event["started_at"].string)?.timeIntervalSince1970 ?? 0
            events.append((t, .system(key: key, name: "系统", content: content, time: event["started_at"].string)))
        }
        events.sort { $0.time < $1.time }

        var result: [CouncilChatMessage] = []
        var cursor = 0
        var previous = -Double.infinity
        for item in speech {
            let started = Self.parsedInstant(item.item["started_at"].string)
            let index = speech.firstIndex(where: { $0.key == item.key }) ?? 0
            let raw = started?.timeIntervalSince1970 ?? (Self.parsedInstant(row["created_at"].string)?.timeIntervalSince1970 ?? 0) - Double(entries.count - index) * 0.001
            let time = max(previous + 0.001, raw)
            previous = time
            while cursor < events.count && events[cursor].time <= time { result.append(events[cursor].message); cursor += 1 }
            let kind = item.item["kind"].string ?? "model"
            let name = kind == "user" ? "我" : (item.item["speaker"].string ?? item.item["model"].string ?? "模型")
            let content = kind == "user" ? (item.item["content"].string ?? "") : publicContent(item.item["content"].string)
            let error = item.item["error"].string
            result.append(.speech(key: "\(prefix):speech:\(item.key)", kind: kind, content: content,
                                  name: name, time: item.item["started_at"].string, error: error?.isEmpty == false ? error : nil,
                                  model: item.item["model"].string, elapsed: item.item["elapsed_ms"].double))
        }
        result.append(contentsOf: events.dropFirst(cursor).map(\.message))
        if let error = row["error"].string, !error.isEmpty {
            result.append(.system(key: "\(prefix):error", name: "系统", content: "失败 / 超时：\(error)", time: row["finished_at"].string))
        }
        return result
    }

    static func conversationStatus(_ row: JSON) -> String {
        let items = transcriptItems(row)
        let modelDone = items.contains { item in
            guard item["kind"].string != "user", item["content"].string?.isEmpty == false else { return false }
            let error = item["error"].string
            return error == nil || error!.isEmpty
        }
        switch row["status"].string {
        case "done": return row["conclusion"].isNull ? (modelDone ? "待主持人总结" : "待模型发言") : "完成"
        case "failed": return modelDone ? "部分完成 · 有失败" : "失败"
        default: return "进行中"
        }
    }

    static func councilIdentity(_ row: JSON) -> String {
        "\(row["id"].int ?? 0):\(row["created_at"].string ?? "legacy")"
    }

    static func evidenceSummary(_ evidence: JSON) -> String {
        let kline = evidence["kline"]
        guard !kline.isNull else { return "" }
        let local = kline["local_count"].int ?? 0
        let pub = kline["public_count"].int ?? 0
        let expected = kline["expected_trading_days"].int ?? 30
        let reason = kline["missing_reason"].string ?? ""
        let summary = "本地 \(local) 根 / 公开 \(pub) 根，预期约 \(expected) 个交易日"
        return reason.isEmpty ? summary : "\(summary)。\(reason)"
    }

    static func groupedWeb(_ evidence: JSON) -> [(String, [JSON])] {
        var groups: [String: [JSON]] = [:]
        for item in evidence["web"].array {
            let key = item["category"].string ?? "other"
            groups[key, default: []].append(item)
        }
        return groups.sorted { a, b in
            if a.0 == "market" { return true }
            if b.0 == "market" { return false }
            return a.0 < b.0
        }
    }

    static func sourceHealthText(_ row: JSON) -> String {
        let health = row["health"]
        if health.isNull { return "尚未验证" }
        if (health["open_until"].double ?? 0) > Date().timeIntervalSince1970 { return "熔断冷却中" }
        return sourceStatusLabel(health["status"].string)
    }
}

private extension JSON {
    var anyHashKey: String {
        [self["kind"].string ?? "", self["model"].string ?? "", self["started_at"].string ?? "",
         self["content"].string ?? "", self["error"].string ?? ""].joined(separator: "|")
    }
}
