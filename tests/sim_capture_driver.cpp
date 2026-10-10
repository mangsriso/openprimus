#include <cerrno>
#include <cstdlib>
#include <unistd.h>

int injected_fsync(int fd) {
  static unsigned calls = 0;
  const char *fail_at = getenv("PRIMUS_TEST_FAIL_FSYNC");
  if (fail_at && ++calls == strtoul(fail_at, nullptr, 10)) {
    errno = EIO;
    return -1;
  }
  return fsync(fd);
}
#define fsync injected_fsync
#include "sim_capture.h"
#undef fsync
#include <iostream>
#include <sstream>

int main(int argc, char **argv) {
  if (argc == 1) {
    auto &bridge = primus_sim::capture();
    primus_sim::Sample sample{9.25f, 2.5f, 9.0f, NAN, true};
    bridge.begin("Classic 9 Bar", sample, 0);
    bridge.end("stop", sample, 1);
    for (uint32_t now = 2;; ++now) {
      bridge.poll(now);
      usleep(1000);
    }
  }
  if (argc != 4) return 2;
  primus_sim::SimCapture bridge(argv[1], strtoull(argv[2], nullptr, 16), uint16_t(atoi(argv[3])));
  primus_sim::Sample sample{9.25f, 2.5f, 9.0f, NAN, true};
  std::string line;
  while (std::getline(std::cin, line)) {
    std::istringstream input(line);
    std::string command;
    uint32_t now;
    input >> command >> now;
    if (command == "begin") bridge.begin("Classic 9 Bar", sample, now);
    else if (command == "end" || command == "back") bridge.end(command, sample, now);
    else if (command == "observe") bridge.observe(sample, now);
    else if (command == "poll") bridge.poll(now);
    else return 3;
    std::cout << bridge.sequence() << " " << bridge.queued() << " " << bridge.dropped()
              << " " << (bridge.error().empty() ? "ok" : "error") << std::endl;
  }
}
