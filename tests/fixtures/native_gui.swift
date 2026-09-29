import AppKit
import Foundation
import Darwin
// Disposable live acceptance target; this app never opens or saves user files.
if CommandLine.arguments.count == 2 {
    let bytes = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
    let image = NSBitmapImageRep(data: bytes)!
    let color = image.colorAt(x: image.pixelsWide / 2, y: image.pixelsHigh / 2)!
        .usingColorSpace(.sRGB)!
    let result: [String: Any] = ["width": image.pixelsWide, "height": image.pixelsHigh,
                               "red": color.redComponent, "blue": color.blueComponent]
    let encoded = try JSONSerialization.data(withJSONObject: result)
    FileHandle.standardOutput.write(encoded)
    exit(0)
}
let application = NSApplication.shared
application.setActivationPolicy(.regular)
final class Controller: NSObject {
    let output = NSTextField(labelWithString: "0")
    @objc func changed(_ sender: NSStepper) { output.stringValue = String(sender.integerValue) }
}
let controller = Controller()
let primary = NSWindow(contentRect: NSRect(x: 120, y: 180, width: 480, height: 320),
                       styleMask: [.titled, .closable], backing: .buffered, defer: false)
primary.title = "Anywhere Native Fixture Red"
primary.isReleasedWhenClosed = false
primary.contentView!.wantsLayer = true
primary.contentView!.layer!.backgroundColor = NSColor.red.cgColor
let stepper = NSStepper(frame: NSRect(x: 30, y: 30, width: 30, height: 28))
stepper.minValue = 0
stepper.maxValue = 100
stepper.increment = 1
stepper.integerValue = 0
stepper.setAccessibilityLabel("Fixture counter")
stepper.setAccessibilityIdentifier("fixture-stepper")
stepper.target = controller
stepper.action = #selector(Controller.changed(_:))
controller.output.frame = NSRect(x: 80, y: 30, width: 200, height: 25)
controller.output.setAccessibilityIdentifier("fixture-result")
primary.contentView!.addSubview(stepper)
primary.contentView!.addSubview(controller.output)
let secondary = NSWindow(contentRect: NSRect(x: 640, y: 180, width: 320, height: 240),
                         styleMask: [.titled, .closable], backing: .buffered, defer: false)
secondary.title = "Anywhere Native Fixture Blue"
secondary.isReleasedWhenClosed = false
secondary.contentView!.wantsLayer = true
secondary.contentView!.layer!.backgroundColor = NSColor.blue.cgColor
primary.orderFrontRegardless()
secondary.orderFrontRegardless()
DispatchQueue.global().async {
    while let command = readLine() {
        if command == "ambiguous" {
            DispatchQueue.main.async {
                secondary.title = primary.title
                secondary.setFrame(primary.frame, display: true)
                print("ambiguous")
                fflush(stdout)
            }
        }
    }
}
print("ready")
fflush(stdout)
application.run()
