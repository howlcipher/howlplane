package main

import (
	"fmt"
	"net/http"
	"time"
)

func helloHandler(w http.ResponseWriter, r *http.Request) {
	fmt.Fprintf(w, "Go Backend is running successfully.")
}

func main() {
	http.HandleFunc("/", helloHandler)
	server := &http.Server{
		Addr:              ":8080",
		ReadHeaderTimeout: 3 * time.Second,
	}
	_ = server.ListenAndServe()
}
