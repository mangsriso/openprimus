import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;
import java.io.ByteArrayInputStream;
import java.io.PrintWriter;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

public class AuditSlim extends GhidraScript {
    private record Segment(int index, long address, int offset, int size) {}

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        Path image = Path.of(args[0]);
        Path output = Path.of(args[1]);
        Files.createDirectories(output);
        byte[] bytes = Files.readAllBytes(image);
        String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
        if (!hash.equals("ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4")) {
            throw new IllegalArgumentException("Expected the reviewed KD360S 1.0.13 HK image");
        }
        ByteBuffer data = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        if ((bytes[0] & 255) != 0xe9 || data.getShort(12) != 9) {
            throw new IllegalArgumentException("Expected an ESP32-S3 application image");
        }
        List<Segment> segments = new ArrayList<>();
        MemoryBlock seed = currentProgram.getMemory().getBlock(toAddr(0));
        if (seed != null) currentProgram.getMemory().removeBlock(seed, monitor);
        int offset = 24;
        for (int index = 0; index < (bytes[1] & 255); index++) {
            long address = Integer.toUnsignedLong(data.getInt(offset));
            int size = data.getInt(offset + 4);
            if (size < 0 || offset + 8L + size > bytes.length) {
                throw new IllegalArgumentException("Truncated segment");
            }
            segments.add(new Segment(index, address, offset + 8, size));
            offset += 8 + size;
        }
        for (Segment segment : segments) {
            MemoryBlock block = currentProgram.getMemory().createInitializedBlock(
                "SEG" + segment.index(), toAddr(segment.address()),
                new ByteArrayInputStream(bytes, segment.offset(), segment.size()),
                segment.size(), monitor, false);
            block.setRead(true);
            block.setWrite(segment.index() == 1);
            block.setExecute(segment.index() >= 2);
            println("Imported " + block.getName() + " " + block.getStart() + " " + segment.size());
        }
        List<String> needles = List.of(
            "HTTP server started", "Pressure: %.1f => %.1f", "Flowrate: %.1f => %.1f",
            "Start Make Coffee", "Scan QR code to access OTA panel", "GOTO OTA MODE",
            "GOTO GOD SEVRER MODE", "Click DFU button to start WIFI AP",
            "/ota/start", "/ota/upload", "src/main.cpp", "src/FunctionRoutine.cpp",
            "src/StateControl.cpp", "src/ui/screens/ui_OTAUpdater.cpp",
            "src/ui/screens/ui_ScanWifi.cpp", "src/ui/screens/ui_GodDoor.cpp",
            "KD-360S_OTA", "KD360S_OTA", "192.168.4.1");
        List<Address> stringAddresses = new ArrayList<>();
        List<String> stringValues = new ArrayList<>();
        for (String needle : needles) {
            byte[] search = (needle + "\0").getBytes(StandardCharsets.US_ASCII);
            for (Segment segment : segments) {
                if (segment.index() >= 2) continue;
                int end = segment.offset() + segment.size() - search.length;
                for (int at = segment.offset(); at <= end; at++) {
                    if (Arrays.equals(bytes, at, at + search.length, search, 0, search.length)) {
                        Address address = toAddr(segment.address() + at - segment.offset());
                        if (getDataAt(address) == null) createAsciiString(address);
                        stringAddresses.add(address);
                        stringValues.add(needle);
                    }
                }
            }
        }
        Address entry = toAddr(Integer.toUnsignedLong(data.getInt(4)));
        addEntryPoint(entry);
        disassemble(entry);
        if (getFunctionAt(entry) == null) createFunction(entry, "app_entry");
        analyzeAll(currentProgram);
        Set<Function> selected = new LinkedHashSet<>();
        try (PrintWriter refs = new PrintWriter(output.resolve("references.txt").toFile())) {
            refs.println("Image SHA256 " + hash);
            for (int index = 0; index < stringAddresses.size(); index++) {
                Address target = stringAddresses.get(index);
                refs.println("STRING " + target + " " + stringValues.get(index));
                for (Reference ref : getReferencesTo(target)) {
                    Function function = getFunctionContaining(ref.getFromAddress());
                    refs.println("  REF " + ref.getFromAddress() + " " + (function == null ? "data" : function.getName()));
                    if (function != null) selected.add(function);
                    for (Reference indirect : getReferencesTo(ref.getFromAddress())) {
                        Function owner = getFunctionContaining(indirect.getFromAddress());
                        if (owner != null) {
                            selected.add(owner);
                            refs.println("    VIA " + indirect.getFromAddress() + " " + owner.getName());
                        }
                    }
                }
            }
            refs.println("FUNCTION_COUNT " + currentProgram.getFunctionManager().getFunctionCount());
        }
        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);
        try (PrintWriter code = new PrintWriter(output.resolve("selected.c").toFile());
             PrintWriter functions = new PrintWriter(output.resolve("functions.tsv").toFile())) {
            for (Function function : currentProgram.getFunctionManager().getFunctions(true)) {
                functions.println(function.getEntryPoint() + "\t" + function.getName() + "\t" + function.getBody().getNumAddresses());
            }
            for (Function function : selected) {
                println("Decompiling " + function.getEntryPoint() + " " + function.getName());
                code.println("\n/* FUNCTION " + function.getEntryPoint() + " " + function.getName() + " */");
                DecompileResults result = decompiler.decompileFunction(function, 45, monitor);
                if (result.decompileCompleted()) code.println(result.getDecompiledFunction().getC());
                else code.println("/* INCOMPLETE " + result.getErrorMessage() + " */");
            }
        } finally {
            decompiler.dispose();
        }
        println("Audit saved to " + output);
    }
}
