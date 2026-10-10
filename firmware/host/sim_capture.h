#pragma once
#include "protocol.h"
#include <arpa/inet.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <unistd.h>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace primus_sim {
struct Sample {
  float pressure{}, flow{}, target_pressure{}, target_flow{};
  bool active{};
};

class SimCapture {
 public:
  SimCapture(const char *journal, uint64_t session, uint16_t port) : session_(session) {
    if (!journal || !*journal) return;
    enabled_ = true;
    journal_ = fopen(journal, "wx");
    if (!journal_) { fail("cannot create lifecycle journal"); return; }
    fd_ = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd_ < 0 || fcntl(fd_, F_SETFL, O_NONBLOCK) < 0) {
      fail("cannot create nonblocking UDP socket"); return;
    }
    endpoint_.sin_family = AF_INET;
    endpoint_.sin_port = htons(port);
    endpoint_.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    char line[160];
    snprintf(line, sizeof(line), "{\"event\":\"run\",\"source\":\"simulation\",\"session\":\"%016llx\",\"version\":1}\n",
             static_cast<unsigned long long>(session_));
    append(line);
  }
  ~SimCapture() {
    if (journal_) fclose(journal_);
    if (fd_ >= 0) close(fd_);
  }
  SimCapture(const SimCapture &) = delete;
  SimCapture &operator=(const SimCapture &) = delete;
  bool enabled() const { return enabled_; }
  const std::string &error() const { return error_; }
  unsigned queued() const { return ring_.head - ring_.tail; }
  unsigned dropped() const { return ring_.dropped; }
  unsigned sequence() const { return ring_.sequence; }

  void begin(const std::string &mode, const Sample &sample, uint32_t now) {
    if (!enabled_ || !error_.empty() || shot_) return;
    shot_ = ++shot_count_;
    boundary("begin", mode, now);
    observe(sample, now);
  }
  void end(const std::string &reason, const Sample &sample, uint32_t now) {
    if (!enabled_ || !error_.empty() || !shot_) return;
    observe(sample, now);
    boundary("end", reason, now);
    if (error_.empty()) {
      char committed[100];
      snprintf(committed, sizeof(committed), "{\"event\":\"end_committed\",\"shot\":%u,\"seq\":%u}\n",
               shot_, ring_.sequence);
      append(committed);
    }
    shot_ = 0;
  }
  void observe(const Sample &sample, uint32_t now) {
    if (!enabled_ || !error_.empty()) return;
    if (ring_.sequence == UINT32_MAX) { fail("sequence exhausted"); return; }
    uint8_t packet[64] = {'W','P','S','1',1,1,64,0};
    put32(packet + 8, uint32_t(session_));
    put32(packet + 12, uint32_t(session_ >> 32));
    put32(packet + 20, now);
    float values[] = {sample.pressure, sample.flow, sample.target_pressure, sample.target_flow};
    for (unsigned i = 0; i < 4; ++i) {
      uint32_t bits;
      memcpy(&bits, &values[i], sizeof(bits));
      put32(packet + 32 + i * 4, bits);
    }
    packet[52] = sample.active;
    put32(packet + 56, 1);
    enqueue(&ring_, packet);
  }
  void poll(uint32_t now) {
    if (!enabled_ || !error_.empty()) return;
    for (unsigned i = 0; i < 32; ++i) {
      uint8_t ack[64];
      sockaddr_in peer{};
      socklen_t size = sizeof(peer);
      auto count = recvfrom(fd_, ack, sizeof(ack), 0, reinterpret_cast<sockaddr *>(&peer), &size);
      if (count < 0) {
        if (!transient(errno)) fail("UDP receive failed");
        break;
      }
      if (peer.sin_addr.s_addr == endpoint_.sin_addr.s_addr && peer.sin_port == endpoint_.sin_port &&
          acknowledge(&ring_, ack, static_cast<unsigned>(count))) sent_ = false;
    }
    const auto *packet = pending(&ring_);
    if (!packet || !error_.empty() || (sent_ && uint32_t(now - last_send_) < 250)) return;
    auto count = sendto(fd_, packet, 64, 0, reinterpret_cast<sockaddr *>(&endpoint_), sizeof(endpoint_));
    if (count != 64) {
      if (!transient(errno)) fail("UDP send failed");
      return;
    }
    sent_ = true;
    last_send_ = now;
  }

 private:
  static bool transient(int error) {
    return error == EAGAIN || error == EWOULDBLOCK || error == EINTR || error == ENOBUFS ||
        error == ECONNREFUSED || error == ENETUNREACH || error == EHOSTUNREACH || error == ENETDOWN;
  }
  static std::string quoted(const std::string &value) {
    std::string result = "\"";
    for (unsigned char c : value) {
      if (c == '"' || c == '\\') result += '\\';
      if (c < 32) {
        char escaped[7]; snprintf(escaped, sizeof(escaped), "\\u%04x", c); result += escaped;
      } else result += char(c);
    }
    return result + '"';
  }
  void fail(const char *message) {
    if (error_.empty()) { error_ = message; fprintf(stderr, "SIMULATION CAPTURE ERROR: %s\n", message); }
  }
  void append(const std::string &line) {
    if (!journal_ || !error_.empty()) return;
    if (fwrite(line.data(), 1, line.size(), journal_) != line.size() || fflush(journal_) || fsync(fileno(journal_)))
      fail("lifecycle journal write failed");
  }
  void boundary(const char *event, const std::string &detail, uint32_t now) {
    char line[240];
    snprintf(line, sizeof(line), "{\"event\":\"%s\",\"shot\":%u,\"seq\":%u,\"dropped\":%u,\"ms\":%u,\"detail\":",
             event, shot_, ring_.sequence, ring_.dropped, now);
    append(std::string(line) + quoted(detail) + "}\n");
  }
  snapshot_ring ring_{};
  uint64_t session_{};
  uint32_t shot_{}, shot_count_{}, last_send_{};
  bool enabled_{}, sent_{};
  FILE *journal_{};
  int fd_{-1};
  sockaddr_in endpoint_{};
  std::string error_;
};

inline SimCapture &capture() {
  static SimCapture instance(getenv("PRIMUS_SIM_JOURNAL"),
      getenv("PRIMUS_SIM_SESSION") ? strtoull(getenv("PRIMUS_SIM_SESSION"), nullptr, 16) : 0,
      getenv("PRIMUS_SIM_PORT") ? uint16_t(strtoul(getenv("PRIMUS_SIM_PORT"), nullptr, 10)) : 47653);
  return instance;
}
}
