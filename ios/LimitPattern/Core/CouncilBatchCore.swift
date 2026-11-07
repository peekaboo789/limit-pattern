import Foundation
import CryptoKit

enum CouncilBatchParseError: LocalizedError, Equatable {
    case fileTooLarge(Int)
    case unsupportedText
    case missingDate
    case invalidDate(String)
    case missingHeader
    case tooFewRows
    case tooManyRows(Int)
    case lineTooLong(Int)
    case tooManyCandidates(Int)

    var errorDescription: String? {
        switch self {
        case .fileTooLarge(let limit): return "文件超过 \(limit) 字节上限"
        case .unsupportedText: return "只支持 UTF-8 / UTF-8 BOM 文本"
        case .missingDate: return "未找到数据日期"
        case .invalidDate(let value): return "数据日期格式无效，应为 YYYY-MM-DD：\(value)"
        case .missingHeader: return "未找到包含代码/名称的表头"
        case .tooFewRows: return "TXT 行数不足，无法识别表头与数据"
        case .tooManyRows(let limit): return "TXT 行数超过 \(limit) 行上限"
        case .lineTooLong(let line): return "第 \(line) 行超过 4096 字符上限"
        case .tooManyCandidates(let limit): return "候选超过 \(limit) 条上限"
        }
    }
}

/// 批量 TXT 导入的纯契约层。只做本地 UTF-8/BOM、大小/行数/行宽校验和候选预览，
/// 不执行文件中的代码或任意 URL；最终以服务端 `/batch/preview` 返回为准。
enum CouncilBatchCore {
    static let maxUploadBytes = 131_072
    static let maxLineLength = 4096
    static let maxRows = 1000
    static let maxCandidates = 100
    static let batchUpgradeMessage = "服务端需升级到支持批量讨论的版本"

    static func decode(_ data: Data) throws -> String {
        guard data.count <= maxUploadBytes else { throw CouncilBatchParseError.fileTooLarge(maxUploadBytes) }
        let withoutBOM: Data
        if data.count >= 3, data[data.startIndex] == 0xEF, data[data.index(after: data.startIndex)] == 0xBB, data[data.index(data.startIndex, offsetBy: 2)] == 0xBF {
            withoutBOM = Data(data.dropFirst(3))
        } else {
            withoutBOM = data
        }
        guard let text = String(data: withoutBOM, encoding: .utf8) else { throw CouncilBatchParseError.unsupportedText }
        return text
    }

    static func lines(_ text: String) -> [String] {
        var rows = text.components(separatedBy: CharacterSet.newlines)
        if rows.last?.isEmpty == true { rows.removeLast() }
        return rows
    }

    static func validate(_ text: String) throws {
        let rows = lines(text)
        if rows.count < 2 { throw CouncilBatchParseError.tooFewRows }
        if rows.count > maxRows { throw CouncilBatchParseError.tooManyRows(maxRows) }
        for (index, line) in rows.enumerated() where line.count > maxLineLength {
            throw CouncilBatchParseError.lineTooLong(index + 1)
        }
    }

    static func preview(_ data: Data, sourceName: String) throws -> JSON {
        let text = try decode(data)
        try validate(text)
        return try preview(text: text, sourceName: sourceName)
    }

    static func preview(text: String, sourceName: String) throws -> JSON {
        let rows = lines(text)
        try validate(text)
        var sourceDate = ""
        var headerLine = 0
        var header: [String: Int] = [:]

        for (number, line) in rows.enumerated() {
            let cols = cells(line)
            if cols.count >= 2, cols[0] == "数据日期", sourceDate.isEmpty { sourceDate = cols[1] }
            if cols.count >= 3, cols[0] == "排名", cols.contains("代码"), cols.contains("名称") {
                var headerMap: [String: Int] = [:]
                for (index, column) in cols.enumerated() where headerMap[column] == nil { headerMap[column] = index }
                header = headerMap
                headerLine = number + 1
                break
            }
        }
        guard !sourceDate.isEmpty else { throw CouncilBatchParseError.missingDate }
        guard headerLine > 0, !header.isEmpty else { throw CouncilBatchParseError.missingHeader }
        guard let codeIndex = header["代码"], let nameIndex = header["名称"] else { throw CouncilBatchParseError.missingHeader }
        guard validDate(sourceDate) else { throw CouncilBatchParseError.invalidDate(sourceDate) }

        var items: [JSON] = []
        var errors: [JSON] = []
        var seen = Set<String>()
        for offset in headerLine..<rows.count {
            let line = rows[offset]
            let cols = cells(line)
            guard !cols.joined().isEmpty else { continue }
            let code = cols.indices.contains(codeIndex) ? cols[codeIndex] : ""
            let name = cols.indices.contains(nameIndex) ? cols[nameIndex] : ""
            guard !code.isEmpty || !name.isEmpty else { continue }
            if items.count + errors.count >= maxCandidates {
                errors.append(.object(["line": .number(Double(offset + 1)), "reason": .string("候选超过 \(maxCandidates) 条上限")]))
                break
            }
            if code.count != 6 || !code.allSatisfy(\.isNumber) {
                errors.append(.object(["line": .number(Double(offset + 1)), "code": .string(code), "name": .string(name), "reason": .string("代码列必须是 6 位数字字符串")]))
                continue
            }
            if seen.contains(code) {
                errors.append(.object(["line": .number(Double(offset + 1)), "code": .string(code), "name": .string(name), "reason": .string("重复代码")]))
                continue
            }
            seen.insert(code)
            items.append(.object([
                "line": .number(Double(offset + 1)),
                "code": .string(code),
                "name": .string(name),
                "market": .null,
                "board": .null,
                "board_label": .null,
                "selected": .bool(true)
            ]))
        }
        return .object([
            "preview_version": .number(1),
            "source_name": .string(sourceName),
            "source_date": .string(sourceDate),
            "default_eval_date": .string(sourceDate),
            "items": .array(items),
            "errors": .array(errors),
            "counts": .object([
                "valid": .number(Double(items.count)),
                "invalid": .number(Double(errors.count)),
                "total": .number(Double(items.count + errors.count))
            ])
        ])
    }

    static func cells(_ line: String) -> [String] {
        var s = line
        if s.hasSuffix("\r") { s.removeLast() }
        return s.components(separatedBy: "\t").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
    }

    static func validDate(_ value: String) -> Bool {
        guard !value.isEmpty else { return false }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.isLenient = false
        guard let date = formatter.date(from: value) else { return false }
        let components = Calendar(identifier: .gregorian).dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", components.year ?? 0, components.month ?? 0, components.day ?? 0) == value
    }

    static func selectedItems(_ preview: JSON) -> [JSON] { preview["items"].array.filter { $0["selected"].bool } }
    static func selectedCodes(_ preview: JSON) -> [String] { selectedItems(preview).compactMap { $0["code"].string } }

    static func idempotencyKey(preview: JSON, evalDate: String, profileIDs: [String]) -> String {
        let material = ["council-batch-v1", preview["source_date"].string ?? "", evalDate,
                        profileIDs.joined(separator: ","), selectedCodes(preview).joined(separator: ",")].joined(separator: "|")
        return SHA256.hash(data: Data(material.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    static func batchStatusLabel(_ value: String?) -> String {
        switch value {
        case "pending": return "待开始"
        case "running": return "运行中"
        case "done": return "完成"
        case "interrupted": return "已中断"
        default: return value?.isEmpty == false ? value! : "—"
        }
    }

    static func itemStatusLabel(_ value: String?) -> String {
        switch value {
        case "pending": return "等待"
        case "running": return "进行中"
        case "done": return "完成"
        case "failed": return "失败"
        case "cancelled": return "已取消"
        case "interrupted": return "已中断"
        default: return value?.isEmpty == false ? value! : "—"
        }
    }

    static func batchProbabilityText(_ item: JSON) -> String {
        if let value = item["probability_percent"].double, value.isFinite {
            return String(format: "约 %.1f%%（主观估计）", value)
        }
        let status = item["details"]["probability_status"].string
        switch status {
        case "not_estimable": return "模型未提供估计"
        case "ambiguous_unit": return "单位不明 · 不可确定"
        case "invalid": return "输出无效 · 不可估计"
        default: return "证据不足 / 不可估计"
        }
    }

    static func headerFileName(_ name: String) -> String {
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
        if name.unicodeScalars.allSatisfy({ allowed.contains($0) }) { return name }
        return name.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "upload.txt"
    }
}
