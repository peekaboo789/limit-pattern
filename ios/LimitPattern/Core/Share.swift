import SwiftUI
import UIKit

struct ShareFile: Identifiable { let id = UUID(); let url: URL }

struct ActivityView: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }
    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {}
}

extension View {
    /// 分享 / 存到「文件」。
    func shareSheet(_ item: Binding<ShareFile?>) -> some View {
        sheet(item: item) { f in ActivityView(url: f.url).presentationDetents([.medium, .large]) }
    }
}
