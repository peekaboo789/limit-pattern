import Foundation
import Observation

struct Server: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var baseURL: String
}

enum ServerURL {
    /// 返回规范化后的 `http(s)://host[:port]`；无法解析时返回 nil。
    static func normalize(_ raw: String) -> String? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return nil }
        let lower = s.lowercased()
        if lower.contains("://") {
            guard lower.hasPrefix("http://") || lower.hasPrefix("https://") else { return nil }
        } else {
            s = "http://" + s
        }
        s = s.lowercased()
        guard let comps = URLComponents(string: s),
              let scheme = comps.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              let host = comps.host, !host.isEmpty else { return nil }
        var out = URLComponents()
        out.scheme = scheme
        out.host = host
        out.port = comps.port
        out.path = Self.pathWithoutAPI(comps.path)
        return out.url?.absoluteString
    }

    static func validate(_ raw: String) -> String? {
        let normalized = normalize(raw)
        guard normalized != nil else { return "地址不对：需要 http/https、主机名，可选端口；例如 http://192.168.1.10:8000" }
        return nil
    }

    private static func pathWithoutAPI(_ path: String) -> String {
        var path = path
        while path.hasSuffix("/") {
            path.removeLast()
        }
        if path.lowercased().hasSuffix("/api") {
            path.removeLast(4)
        }
        return path
    }
}

@MainActor
@Observable
final class ServerStore {
    static let storageKey = "limitpattern.servers.v1"
    static let selectedIDKey = "limitpattern.selectedServerID.v1"

    private(set) var servers: [Server] = []
    var selectedID: String?

    let defaults: UserDefaults
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    init(defaults: UserDefaults = .standard, encoder: JSONEncoder = JSONEncoder(), decoder: JSONDecoder = JSONDecoder()) {
        self.defaults = defaults
        self.encoder = encoder
        self.decoder = decoder
        load()
    }

    var selected: Server? { servers.first { $0.id == selectedID } ?? servers.first }

    var currentURL: URL? {
        guard let base = selected?.baseURL else { return nil }
        return APIClient.normalize(base)
    }

    var allURLs: [URL] { servers.compactMap { APIClient.normalize($0.baseURL) } }

    func load() {
        if let data = defaults.data(forKey: Self.storageKey),
           let list = try? decoder.decode([Server].self, from: data) {
            servers = list
        }
        let stored = defaults.string(forKey: Self.selectedIDKey)
        selectedID = servers.contains(where: { $0.id == stored }) ? stored : servers.first?.id
    }

    func persist() {
        defaults.set((try? encoder.encode(servers)), forKey: Self.storageKey)
        defaults.set(selectedID, forKey: Self.selectedIDKey)
    }

    @discardableResult
    func upsert(id: String? = nil, name: String, baseURL: String) throws -> Server {
        guard let normalized = ServerURL.normalize(baseURL) else {
            throw APIError(status: 0, message: ServerURL.validate(baseURL) ?? "服务器地址不对")
        }
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let server = Server(id: id ?? UUID().uuidString,
                            name: trimmedName.isEmpty ? normalized : trimmedName,
                            baseURL: normalized)
        if let index = servers.firstIndex(where: { $0.id == server.id }) {
            servers[index] = server
        } else {
            servers.append(server)
        }
        if id == nil || selectedID == id { selectedID = server.id }
        persist()
        return server
    }

    func remove(id: String) {
        guard let index = servers.firstIndex(where: { $0.id == id }) else { return }
        servers.remove(at: index)
        if selectedID == id { selectedID = servers.first?.id }
        persist()
    }

    func select(id: String) {
        guard servers.contains(where: { $0.id == id }) else { return }
        selectedID = id
        persist()
    }
}
