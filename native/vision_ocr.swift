// Apple Vision OCR helper: native macOS document recognition, no external models.
// Usage: vision_ocr [--langs it-IT,en-US] image1.png [image2.png ...]
// (.fast recognition is not offered: on macOS 27 it returns no text for full pages.)
// Prints one JSON object per line (JSONL), one per image, in input order, then a last line
// {"peak_footprint": bytes} with this process's kernel-tracked lifetime peak memory.
import Foundation
import Vision

struct Box: Codable { let x: Double; let y: Double; let w: Double; let h: Double }
struct Line: Codable { let text: String; let confidence: Float; let box: Box }
struct Block: Codable {
    let kind: String            // "paragraph" | "table" | "list"
    let box: Box
    let text: String?
    let rows: [[String]]?
    let items: [String]?
}
struct PageResult: Codable {
    let image: String
    let lines: [Line]
    let blocks: [Block]
    let error: String?
}

// Vision uses bottom-left origin; convert to top-left so callers sort naturally.
func box(_ r: NormalizedRect) -> Box {
    let c = r.cgRect
    return Box(x: c.origin.x, y: 1 - c.origin.y - c.height, w: c.width, h: c.height)
}

func recognize(_ url: URL, langs: [Locale.Language]) async -> PageResult {
    do {
        var textReq = RecognizeTextRequest()
        textReq.recognitionLanguages = langs
        textReq.recognitionLevel = .accurate
        textReq.usesLanguageCorrection = true
        let textObs = try await textReq.perform(on: url)
        let lines = textObs.compactMap { o -> Line? in
            guard let c = o.topCandidates(1).first else { return nil }
            return Line(text: c.string, confidence: c.confidence, box: box(o.boundingBox))
        }

        var blocks: [Block] = []
        do {
            var docReq = RecognizeDocumentsRequest()
            docReq.textRecognitionOptions.recognitionLanguages = langs
            docReq.textRecognitionOptions.useLanguageCorrection = true
            if let doc = try await docReq.perform(on: url).first?.document {
                for t in doc.tables {
                    let rows = t.rows.map { row in row.map { $0.content.text.transcript } }
                    blocks.append(Block(kind: "table", box: box(t.boundingRegion.boundingBox), text: nil, rows: rows, items: nil))
                }
                for l in doc.lists {
                    blocks.append(Block(kind: "list", box: box(l.boundingRegion.boundingBox), text: nil, rows: nil,
                                        items: l.items.map { $0.itemString }))
                }
                for p in doc.paragraphs {
                    blocks.append(Block(kind: "paragraph", box: box(p.boundingRegion.boundingBox), text: p.transcript, rows: nil, items: nil))
                }
            }
        }
        return PageResult(image: url.path, lines: lines, blocks: blocks, error: nil)
    } catch {
        return PageResult(image: url.path, lines: [], blocks: [], error: "\(error)")
    }
}

var args = Array(CommandLine.arguments.dropFirst())
var langs = [Locale.Language(identifier: "it-IT"), Locale.Language(identifier: "en-US")]
if let i = args.firstIndex(of: "--langs"), i + 1 < args.count {
    langs = args[i + 1].split(separator: ",").map { Locale.Language(identifier: String($0)) }
    args.removeSubrange(i...i + 1)
}

let encoder = JSONEncoder()
for path in args {
    let result = await recognize(URL(fileURLWithPath: path), langs: langs)
    let data = try! encoder.encode(result)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

var usage = rusage_info_v4()
let rc = withUnsafeMutablePointer(to: &usage) { ptr in
    ptr.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) { proc_pid_rusage(getpid(), RUSAGE_INFO_V4, $0) }
}
if rc == 0 {
    FileHandle.standardOutput.write("{\"peak_footprint\":\(usage.ri_lifetime_max_phys_footprint)}\n".data(using: .utf8)!)
}
