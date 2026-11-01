import XCTest
@testable import LimitPattern

final class InMemoryKeychainStore: KeychainStoring, @unchecked Sendable {
    private(set) var values: [String: String] = [:]
    func set(_ value: String?, for key: String) { values[key] = value }
    func get(_ key: String) -> String? { values[key] }
    func remove(_ key: String) { values[key] = nil }
}

@MainActor
final class ServerAccountTests: XCTestCase {
    var defaults: UserDefaults!
    var suiteName: String!

    override func setUp() {
        super.setUp()
        suiteName = "LimitPatternTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
        defaults.removePersistentDomain(forName: suiteName)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        super.tearDown()
    }

    func testServerURLNormalizeAndValidate() {
        XCTAssertEqual(ServerURL.normalize("https://gp.peekaboo789.site:2096/api"), "https://gp.peekaboo789.site:2096")
        XCTAssertEqual(ServerURL.normalize("192.168.1.10:12315/"), "http://192.168.1.10:12315")
        XCTAssertEqual(ServerURL.normalize("HTTP://a.b.c/api/"), "http://a.b.c")
        XCTAssertNil(ServerURL.normalize("ftp://a.b"))
        XCTAssertNil(ServerURL.normalize("http://"))
        XCTAssertNotNil(ServerURL.validate("  "))
    }

    func testServerStoreUpsertSelectRemove() throws {
        let store = ServerStore(defaults: defaults)
        let server = try store.upsert(name: "家", baseURL: "https://a.b:2096/api")
        XCTAssertEqual(server.baseURL, "https://a.b:2096")
        XCTAssertEqual(store.servers.count, 1)
        XCTAssertEqual(store.selectedID, server.id)

        let edited = try store.upsert(id: server.id, name: "公司", baseURL: "http://192.168.1.2:8000")
        XCTAssertEqual(store.servers.count, 1)
        XCTAssertEqual(store.servers[0].name, "公司")
        XCTAssertEqual(edited.baseURL, "http://192.168.1.2:8000")

        let server2 = try store.upsert(name: "备用", baseURL: "https://c.d")
        XCTAssertEqual(store.servers.count, 2)
        XCTAssertEqual(store.selectedID, server2.id)
        store.select(id: server.id)
        XCTAssertEqual(store.selectedID, server.id)
        store.remove(id: server.id)
        XCTAssertEqual(store.selectedID, server2.id)
    }

    func testAccountStoreKeychainIsolationAndRemoval() {
        let keychain = InMemoryKeychainStore()
        let store = AccountStore(keychain: keychain, defaults: defaults)
        store.save(serverID: "s1", username: "alice", password: "p1", cookie: "c1")
        store.save(serverID: "s1", username: "bob", password: "p2", cookie: nil)
        store.save(serverID: "s2", username: "alice", password: "p3", cookie: "c3")

        XCTAssertEqual(keychain.values.count, 5)
        XCTAssertEqual(store.accounts.count, 3)
        XCTAssertEqual(store.password(forServerID: "s1", username: "alice"), "p1")
        XCTAssertEqual(store.cookie(forServerID: "s1", username: "bob"), nil)

        store.remove(serverID: "s1", username: "alice")
        XCTAssertEqual(store.accounts.count, 2)
        XCTAssertNil(keychain.values["password:s1+alice"])
        XCTAssertNil(keychain.values["cookie:s1+alice"])

        store.removeAll(forServerID: "s1")
        XCTAssertEqual(store.accounts.count, 1)
        XCTAssertTrue(store.accounts(forServerID: "s1").isEmpty)
    }

    func testMigrationCreatesFirstServerAndAccount() {
        defaults.set("https://old.example.com:2096/api", forKey: "server")
        defaults.set("alice", forKey: "username")

        let keychain = InMemoryKeychainStore()
        keychain.set("old-cookie", for: "cookie:https://old.example.com:2096")
        let serverStore = ServerStore(defaults: defaults)
        let accountStore = AccountStore(keychain: keychain, defaults: defaults)
        let state = AppState(servers: serverStore, accounts: accountStore, api: APIClient(storage: .shared, keychain: keychain))

        XCTAssertEqual(serverStore.servers.count, 1)
        XCTAssertEqual(serverStore.servers.first?.baseURL, "https://old.example.com:2096")
        XCTAssertEqual(accountStore.accounts.count, 1)
        XCTAssertEqual(accountStore.accounts.first?.username, "alice")
        XCTAssertEqual(accountStore.cookie(forServerID: serverStore.servers.first!.id, username: "alice"), "old-cookie")
        XCTAssertTrue(defaults.bool(forKey: "limitpattern.migratedToServers.v1"))
    }

    func testMigrationIsIdempotent() {
        defaults.set("http://old.local:8000", forKey: "server")
        defaults.set("bob", forKey: "username")
        let keychain = InMemoryKeychainStore()
        let serverStore = ServerStore(defaults: defaults)
        let accountStore = AccountStore(keychain: keychain, defaults: defaults)
        _ = AppState(servers: serverStore, accounts: accountStore, api: APIClient(storage: .shared, keychain: keychain))
        _ = AppState(servers: serverStore, accounts: accountStore, api: APIClient(storage: .shared, keychain: keychain))
        XCTAssertEqual(serverStore.servers.count, 1)
        XCTAssertEqual(accountStore.accounts.count, 1)
    }
}
