import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Path;

public class InventorySlim extends GhidraScript {
    public void run() throws Exception {
        Path output = Path.of(getScriptArgs()[0]);
        Files.createDirectories(output);
        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);
        try (PrintWriter functions = new PrintWriter(output.resolve("functions.tsv").toFile());
             PrintWriter edges = new PrintWriter(output.resolve("calls.tsv").toFile());
             PrintWriter code = new PrintWriter(output.resolve("application.c").toFile())) {
            functions.println("entry\tname\tbytes");
            edges.println("owner\tsite\tflow\ttarget\treference_type");
            for (Function function : currentProgram.getFunctionManager().getFunctions(true)) {
                monitor.checkCancelled();
                functions.println(function.getEntryPoint() + "\t" + function.getName()
                    + "\t" + function.getBody().getNumAddresses());
                var instructions = currentProgram.getListing().getInstructions(function.getBody(), true);
                while (instructions.hasNext()) {
                    var instruction = instructions.next();
                    if (!instruction.getFlowType().isCall()) continue;
                    boolean referenced = false;
                    for (Reference reference : instruction.getReferencesFrom()) {
                        if (!reference.getReferenceType().isCall()) continue;
                        edges.println(function.getEntryPoint() + "\t" + instruction.getAddress()
                            + "\t" + instruction.getMnemonicString() + "\t"
                            + reference.getToAddress() + "\t" + reference.getReferenceType());
                        referenced = true;
                    }
                    if (!referenced) edges.println(function.getEntryPoint() + "\t"
                        + instruction.getAddress() + "\t" + instruction.getMnemonicString()
                        + "\tUNRESOLVED\t" + instruction.getFlowType());
                }
                long entry = function.getEntryPoint().getOffset();
                if (entry < 0x42003000L || entry >= 0x42024344L) continue;
                code.println("\nFUNCTION " + function.getEntryPoint());
                var result = decompiler.decompileFunction(function, 30, monitor);
                if (result.decompileCompleted()) code.println(result.getDecompiledFunction().getC());
                else code.println("INCOMPLETE " + result.getErrorMessage());
            }
        } finally {
            decompiler.dispose();
        }
    }
}
