import Foundation
import Observation

struct SavedAccount: Codable, Hashable, Identifiable {
    var id: String { accountKey }
    var serverID: String
    var username: String
    var lastUsed: Date

    var accountKey: String { "\(serverID)+\(username)" }
}

@MainActor
@Observable
final class AccountStore {
    static let storageKey = "limitpattern.accounts.v1"

    private(set) var accounts: [SavedAccount] = []
    let keychain: KeychainStoring
    let defaults: UserDefaults
    let encoder: JSONEncoder
    let decoder: JSONDecoder

    init(keychain: KeychainStoring = KeychainStore.shared,
         defaults: UserDefaults = .standard,
         encoder: JSONEncoder = JSONEncoder(),
         decoder: JSONDecoder = JSONDecoder()) {
        self.keychain = keychain
        self.defaults = defaults
        self.encoder = encoder
        self.decoder = decoder
        load()
    }

    func load() {
        guard let data = defaults.data(forKey: Self.storageKey),
              let list = try? decoder.decode([SavedAccount].self, from: data) else { return }
        accounts = list
    }

    func persist() {
        defaults.set((try? encoder.encode(accounts)), forKey: Self.storageKey)
    }

    func save(serverID: String, username: String, password: String?, cookie: String?) {
        guard !serverID.isEmpty, !username.isEmpty else { return }
        let key = SavedAccount(serverID: serverID, username: username, lastUsed: Date()).accountKey
        if let password, !password.isEmpty {
            keychain.set(password, for: "password:\(key)")
        } else {
            keychain.remove("password:\(key)")
        }
        if let cookie, !cookie.isEmpty {
            keychain.set(cookie, for: "cookie:\(key)")
        } else {
            keychain.remove("cookie:\(key)")
        }
        let entry = SavedAccount(serverID: serverID, username: username, lastUsed: Date())
        if let index = accounts.firstIndex(where: { $0.serverID == serverID && $0.username == username }) {
            accounts[index] = entry
        } else {
            accounts.append(entry)
        }
        persist()
    }

    func remove(serverID: String, username: String) {
        let key = SavedAccount(serverID: serverID, username: username, lastUsed: Date()).accountKey
        keychain.remove("password:\(key)")
        keychain.remove("cookie:\(key)")
        accounts.removeAll { $0.serverID == serverID && $0.username == username }
        persist()
    }

    func password(forServerID serverID: String, username: String) -> String? {
        keychain.get("password:\(SavedAccount(serverID: serverID, username: username, lastUsed: Date()).accountKey)")
    }

    func cookie(forServerID serverID: String, username: String) -> String? {
        keychain.get("cookie:\(SavedAccount(serverID: serverID, username: username, lastUsed: Date()).accountKey)")
    }

    func updateCookie(serverID: String, username: String, cookie: String?) {
        let key = SavedAccount(serverID: serverID, username: username, lastUsed: Date()).accountKey
        keychain.set(cookie, for: "cookie:\(key)")
    }

    func touch(serverID: String, username: String) {
        guard let index = accounts.firstIndex(where: { $0.serverID == serverID && $0.username == username }) else { return }
        accounts[index].lastUsed = Date()
        persist()
    }

    func accounts(forServerID serverID: String) -> [SavedAccount] {
        accounts.filter { $0.serverID == serverID }
            .sorted { $0.lastUsed > $1.lastUsed }
    }

    func removeAll(forServerID serverID: String) {
        for account in accounts(forServerID: serverID) {
            let key = account.accountKey
            keychain.remove("password:\(key)")
            keychain.remove("cookie:\(key)")
        }
        accounts.removeAll { $0.serverID == serverID }
        persist()
    }
}
