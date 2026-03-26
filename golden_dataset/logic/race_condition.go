// 预期发现：LOGIC 竞态条件 / goroutine 问题
package main

import (
	"fmt"
	"sync"
)

// 危险：多个 goroutine 并发读写共享变量，无锁保护
var counter int

func incrementUnsafe(wg *sync.WaitGroup) {
	defer wg.Done()
	for i := 0; i < 1000; i++ {
		counter++ // 竞态条件：counter 未加锁，并发写会导致数据竞争
	}
}

// 危险：goroutine 泄漏 —— channel 永远没有接收方
func leakGoroutine() {
	ch := make(chan int) // 无缓冲 channel
	go func() {
		ch <- 42 // 永远阻塞，goroutine 泄漏
	}()
	// 忘记从 ch 读取
}

// 危险：忽略 error 返回值
func riskyOperation() {
	result, _ := fmt.Println("hello") // 丢弃 error，_ 屏蔽了潜在问题
	_ = result
}

func main() {
	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go incrementUnsafe(&wg)
	}
	wg.Wait()
	fmt.Println("counter:", counter)
}
