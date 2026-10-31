import Foundation

/// 动态 JSON：服务器字段多且随版本增加，用它避免严格解码失败。
enum JSON: Equatable, Codable {
    case null, bool(Bool), number(Double), string(String), array([JSON]), object([String: JSON])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSON].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSON].self)) }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .number(let n): try c.encode(n)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }
    static func parse(_ data: Data) throws -> JSON { try JSONDecoder().decode(JSON.self, from: data) }

    subscript(key: String) -> JSON { if case .object(let o) = self { return o[key] ?? .null }; return .null }
    subscript(i: Int) -> JSON { if case .array(let a) = self, i >= 0, i < a.count { return a[i] }; return .null }

    var array: [JSON] { if case .array(let a) = self { return a }; return [] }
    var object: [String: JSON] { if case .object(let o) = self { return o }; return [:] }
    var isNull: Bool { self == .null }
    var double: Double? {
        switch self { case .number(let n): return n; case .string(let s): return Double(s); case .bool(let b): return b ? 1 : 0; default: return nil }
    }
    var int: Int? { double.map { Int($0) } }
    var bool: Bool { switch self { case .bool(let b): return b; case .number(let n): return n != 0; case .string(let s): return s == "true"; default: return false } }
    var string: String? {
        switch self {
        case .string(let s): return s
        case .number(let n): return n == n.rounded() && abs(n) < 1e15 ? String(Int(n)) : String(n)
        case .bool(let b): return b ? "true" : "false"
        default: return nil
        }
    }
    /// 显示用文本，空值显示「—」。
    var text: String { (string?.isEmpty == false) ? string! : "—" }
    func fmt(_ digits: Int = 2) -> String { double.map { String(format: "%.\(digits)f", $0) } ?? "—" }
    var any: Any {
        switch self {
        case .null: return NSNull()
        case .bool(let b): return b
        case .number(let n): return n
        case .string(let s): return s
        case .array(let a): return a.map { $0.any }
        case .object(let o): return o.mapValues { $0.any }
        }
    }
    static func from(_ any: Any?) -> JSON {
        guard let any else { return .null }
        if let data = try? JSONSerialization.data(withJSONObject: ["v": any]), let j = try? JSON.parse(data) { return j["v"] }
        return .null
    }
}
