import Foundation

/// 兼容旧调用；新代码请直接用 `KeychainStore`。
enum Keychain {
    static let service = KeychainStore.service
    static let store: KeychainStoring = KeychainStore.shared

    static func set(_ value: String?, for key: String) { store.set(value, for: key) }
    static func get(_ key: String) -> String? { store.get(key) }
    static func remove(_ key: String) { store.remove(key) }
}
