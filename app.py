<!DOCTYPE html>
<html>
<head>
    <title>Payroll Validator</title>
    <style>
        body {
            font-family: Arial;
            background: #f4f6f9;
            padding: 30px;
        }

        h2 {
            color: #333;
        }

        .card {
            background: white;
            padding: 20px;
            border-radius: 10px;
            margin-top: 20px;
            box-shadow: 0 5px 10px rgba(0,0,0,0.1);
        }

        button {
            padding: 10px 15px;
            border: none;
            background: #007bff;
            color: white;
            border-radius: 5px;
            cursor: pointer;
        }

        .stats {
            display: flex;
            gap: 20px;
            margin-top: 20px;
        }

        .box {
            padding: 15px;
            border-radius: 8px;
            color: white;
            flex: 1;
            text-align: center;
        }

        .total { background: #6c757d; }
        .valid { background: #28a745; }
        .invalid { background: #dc3545; }

        .list {
            margin-top: 20px;
        }

        .item {
            padding: 10px;
            margin-bottom: 8px;
            border-radius: 6px;
            background: #fff;
            box-shadow: 0 2px 5px rgba(0,0,0,0.05);
        }

        .match { border-left: 5px solid green; }
        .invalid-row { border-left: 5px solid red; }
    </style>
</head>
<body>

<h2>🚀 Payroll Validator</h2>

<div class="card">
    <input type="file" id="fileInput" />
    <button onclick="uploadFile()">VALIDASI</button>
    <button onclick="downloadExcel()">⬇️ Download</button>
</div>

<div class="stats">
    <div class="box total">Total: <span id="total">0</span></div>
    <div class="box valid">Valid: <span id="valid">0</span></div>
    <div class="box invalid">Invalid: <span id="invalid">0</span></div>
</div>

<div class="list" id="list"></div>

<script>
let globalData = [];

async function uploadFile() {
    const fileInput = document.getElementById("fileInput").files[0];

    if (!fileInput) {
        alert("Pilih file dulu!");
        return;
    }

    const formData = new FormData();
    formData.append("file", fileInput);

    const res = await fetch("/upload", {
        method: "POST",
        body: formData
    });

    const data = await res.json();
    globalData = data;

    let total = data.length;
    let valid = 0;
    let invalid = 0;

    let listHTML = "";

    data.forEach(row => {
        if (row.status === "MATCH") valid++;
        else invalid++;

        listHTML += `
            <div class="item ${row.status === "MATCH" ? 'match' : 'invalid-row'}">
                <b>${row.nama}</b> - ${row.bank} - ${row.rekening}
                <br>Status: ${row.status}
            </div>
        `;
    });

    document.getElementById("total").innerText = total;
    document.getElementById("valid").innerText = valid;
    document.getElementById("invalid").innerText = invalid;
    document.getElementById("list").innerHTML = listHTML;
}

function downloadExcel() {
    if (globalData.length === 0) {
        alert("Belum ada data!");
        return;
    }

    let csv = "Nama,Rekening,Bank,Nama Bank,Status\n";

    globalData.forEach(row => {
        csv += `${row.nama},${row.rekening},${row.bank},${row.nama_bank},${row.status}\n`;
    });

    const blob = new Blob([csv], { type: "text/csv" });
    const url = window.URL.createObjectURL(blob);

    const a = document.createElement("a");
    a.href = url;
    a.download = "hasil_validasi.csv";
    a.click();
}
</script>

</body>
</html>
