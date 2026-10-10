import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import java.io.PrintWriter;
import java.util.LinkedHashSet;
import java.util.Set;

public class TraceSlim extends GhidraScript {
    public void run() throws Exception {
        String[] args = getScriptArgs();
        Set<Function> selected = new LinkedHashSet<>();
        try (PrintWriter out = new PrintWriter(args[0])) {
            for (int i = 1; i < args.length; i++) {
                Address target = toAddr(Long.parseUnsignedLong(args[i], 16));
                out.println("TARGET " + target);
                Function direct = getFunctionAt(target);
                var block = currentProgram.getMemory().getBlock(target);
                if (direct == null && block != null && block.isExecute()
                        && block.contains(target.add(2))
                        && getByte(target) == 0x36 && getByte(target.add(2)) == 0) {
                    disassemble(target);
                    direct = createFunction(target, null);
                }
                if (direct != null) selected.add(direct);
                Set<Address> frontier = new LinkedHashSet<>();
                frontier.add(target);
                Set<Address> visited = new LinkedHashSet<>();
                for (int depth = 0; depth < 3; depth++) {
                    Set<Address> next = new LinkedHashSet<>();
                    for (Address address : frontier) {
                        if (!visited.add(address)) continue;
                        for (Reference ref : getReferencesTo(address)) {
                            Function owner = getFunctionContaining(ref.getFromAddress());
                            out.println("REF " + depth + " " + address + " FROM "
                                + ref.getFromAddress() + " OWNER "
                                + (owner == null ? "data" : owner.getEntryPoint()));
                            if (owner == null) next.add(ref.getFromAddress());
                            else selected.add(owner);
                        }
                    }
                    frontier = next;
                }
            }
            DecompInterface decompiler = new DecompInterface();
            decompiler.openProgram(currentProgram);
            try {
                for (Function function : selected) {
                    out.println("\nFUNCTION " + function.getEntryPoint());
                    out.println("CALLEES " + function.getCalledFunctions(monitor));
                    out.println("CALLERS " + function.getCallingFunctions(monitor));
                    var result = decompiler.decompileFunction(function, 60, monitor);
                    if (result.decompileCompleted()) out.println(result.getDecompiledFunction().getC());
                    else out.println("INCOMPLETE " + result.getErrorMessage());
                }
            } finally {
                decompiler.dispose();
            }
        }
    }
}
