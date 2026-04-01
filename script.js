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

    let table = "<tr><th>Nama</th><th>Rekening</th><th>Bank</th><th>Nama Bank</th><th>Status</th></tr>";

    data.forEach(row => {
        let color = "black";

        if (row.status === "MATCH") color = "green";
        if (row.status === "MIRIP") color = "orange";
        if (row.status === "SALAH" || row.status === "INVALID") color = "red";

        table += `<tr style="color:${color}">
            <td>${row.nama}</td>
            <td>${row.rekening}</td>
            <td>${row.bank}</td>
            <td>${row.nama_bank}</td>
            <td>${row.status}</td>
        </tr>`;
    });

    document.getElementById("resultTable").innerHTML = table;
}
