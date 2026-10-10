import ghidra.app.script.GhidraScript;
import java.io.PrintWriter;

public class DumpCapture extends GhidraScript {
    public void run() throws Exception {
        String[] args = getScriptArgs();
        try (PrintWriter out = new PrintWriter(args[0])) {
            for (int i = 1; i < args.length; i++) {
                var address = toAddr(Long.parseUnsignedLong(args[i], 16));
                out.println("ADDRESS " + address);
                if (currentProgram.getMemory().contains(address)) {
                    out.println("WORD " + Integer.toUnsignedString(getInt(address), 16));
                }
                var function = getFunctionAt(address);
                if (function == null) {
                    disassemble(address);
                    function = createFunction(address, null);
                }
                if (function != null) {
                    for (var instruction : currentProgram.getListing().getInstructions(function.getBody(), true)) {
                        out.println(instruction.getAddress() + " " + instruction);
                    }
                }
            }
        }
    }
}
